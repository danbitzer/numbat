from datetime import UTC, datetime

import pytest
from conftest import FakeHa, fake_ha_client, load_fixture_state

from numbat.adapters.amber import AmberExpressAdapter, PriceParseError, parse_forecast_attribute
from numbat.adapters.solar import OpenMeteoSolarAdapter, parse_watts_attribute
from numbat.adapters.weather import WeatherAdapter
from numbat.config import Entities

ENTITIES = Entities(
    buy_price="sensor.amber_express_general_price",
    sell_price="sensor.amber_express_feed_in_price",
    price_spike="binary_sensor.amber_express_price_spike",
    pv_forecast_today="sensor.home_energy_production_today",
    pv_forecast_tomorrow="sensor.home_energy_production_tomorrow",
    battery_soc="sensor.battery_level",
    battery_power="sensor.battery_power",
    weather="weather.henley_beach_hourly",
)


def test_parse_feed_in_forecast_fixture():
    series = parse_forecast_attribute(load_fixture_state("amber_express_feed_in_price"))
    assert len(series.times) == 68  # 9 x 5-min near-term + 59 x 30-min
    # 20:50 +09:30 == 11:20 UTC; positive = export revenue, $/kWh, no sign flip
    assert series.times[0] == datetime(2026, 7, 15, 11, 20, tzinfo=UTC)
    assert series.values[0] == pytest.approx(0.1585)
    assert all(t.tzinfo == UTC for t in series.times)
    # Mixed native resolution on a 5-min site: 5-min near-term, 30-min beyond
    assert (series.times[1] - series.times[0]).total_seconds() == 300
    assert (series.times[-1] - series.times[-2]).total_seconds() == 1800
    # Tomorrow evening's high prices survive parsing (the spike-reserve driver)
    assert max(series.values) == pytest.approx(0.6505)


def test_parse_forecast_rejects_non_amber_sensor():
    state = load_fixture_state("solar_production_today")
    with pytest.raises(PriceParseError, match="forecast"):
        parse_forecast_attribute(state)


def test_parse_solar_watts_fixture():
    points = parse_watts_attribute(load_fixture_state("solar_production_today"))
    assert len(points) == 96  # 24h of 15-min points
    # 13:00 +09:30 == 03:30 UTC, 8942 W -> 8.942 kW
    assert points[datetime(2026, 7, 15, 3, 30, tzinfo=UTC)] == pytest.approx(8.942)
    assert min(points.values()) == 0.0


def test_parse_general_forecast_fixture():
    series = parse_forecast_attribute(load_fixture_state("amber_express_general_price"))
    # 21:05 +09:30 == 11:35 UTC; 5-min near-term then 30-min
    assert series.times[0] == datetime(2026, 7, 15, 11, 35, tzinfo=UTC)
    assert series.values[0] == pytest.approx(0.44)
    assert (series.times[1] - series.times[0]).total_seconds() == 300
    assert (series.times[-1] - series.times[-2]).total_seconds() == 1800
    # Tomorrow evening peak (20:00 ACST == 10:30 UTC): $1.07/kWh
    assert max(series.values) == pytest.approx(1.0676)


def test_buy_sell_spread_positive_everywhere():
    """Amber invariant the MILP relies on: sell < buy in overlapping intervals."""
    buy = parse_forecast_attribute(load_fixture_state("amber_express_general_price"))
    sell = parse_forecast_attribute(load_fixture_state("amber_express_feed_in_price"))
    buy_by_time = dict(zip(buy.times, buy.values, strict=False))
    overlaps = [t for t in sell.times if t in buy_by_time]
    assert len(overlaps) > 50
    assert all(
        buy_by_time[t] > s
        for t, s in zip(sell.times, sell.values, strict=False)
        if t in buy_by_time
    )


async def test_amber_express_adapter_end_to_end():
    fake = FakeHa()
    fake.add_fixture("amber_express_feed_in_price")
    fake.add_fixture("amber_express_general_price")
    fake.add_fixture("amber_express_price_spike")

    async with fake_ha_client(fake) as client:
        prices = await AmberExpressAdapter(client, ENTITIES).get_prices()

    assert prices.current_buy == pytest.approx(0.44)
    assert prices.current_sell == pytest.approx(0.1585)
    assert prices.live_spike is False  # fixture: state off, spike_status none
    assert prices.sell.values[0] == pytest.approx(0.1585)
    assert prices.updated_at is not None
    assert prices.current_estimate is False  # fixture: estimate: false


async def test_amber_express_adapter_flags_unconfirmed_price():
    fake = FakeHa()
    fake.add_fixture("amber_express_feed_in_price")
    fake.add_fixture("amber_express_general_price")
    fake.add_fixture("amber_express_price_spike")
    # right after an interval starts the sensor carries the forecast value
    fake.states["sensor.amber_express_general_price"]["attributes"]["estimate"] = True

    async with fake_ha_client(fake) as client:
        prices = await AmberExpressAdapter(client, ENTITIES).get_prices()
    assert prices.current_estimate is True


async def test_weather_adapter_hourly_temps():
    fake = FakeHa()
    fake.add_fixture("weather_henley_beach_hourly")
    fake.service_responses[("weather", "get_forecasts")] = {
        "weather.henley_beach_hourly": {
            "forecast": [
                {"datetime": "2026-07-15T21:00:00+09:30", "temperature": 6.5},
                {"datetime": "2026-07-15T22:00:00+09:30", "temperature": 5.9},
                {"datetime": "2026-07-15T23:00:00+09:30", "temperature": 5.4},
            ]
        }
    }
    async with fake_ha_client(fake) as client:
        temps = await WeatherAdapter(client, ENTITIES).get_temperature_forecast()

    assert temps.times[0] == datetime(2026, 7, 15, 11, 30, tzinfo=UTC)
    assert temps.values == [6.5, 5.9, 5.4]
    domain, service, data = fake.service_calls[0]
    assert (domain, service) == ("weather", "get_forecasts")
    assert data == {"entity_id": "weather.henley_beach_hourly", "type": "hourly"}


async def test_solar_adapter_merges_today_and_tomorrow():
    fake = FakeHa()
    fake.add_fixture("solar_production_today")
    fake.add_fixture("solar_production_tomorrow")

    async with fake_ha_client(fake) as client:
        pv = await OpenMeteoSolarAdapter(client, ENTITIES).get_pv()

    assert len(pv.times) == 192  # two days of 15-min points
    assert pv.times == sorted(pv.times)
    # Tomorrow midday (12:30 +09:30 == 03:00 UTC on the 16th): 8976 W
    idx = pv.times.index(datetime(2026, 7, 16, 3, 0, tzinfo=UTC))
    assert pv.values[idx] == pytest.approx(8.976)
