from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import cast
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from conftest import FakeHa, fake_ha_client

from numbat.adapters.amber import AmberExpressAdapter
from numbat.adapters.solar import OpenMeteoSolarAdapter
from numbat.adapters.sungrow import SungrowAdapter
from numbat.adapters.weather import WeatherAdapter
from numbat.config import Settings
from numbat.models import Action, BatteryState, Plan, PlanInterval, PriceForecast, Series
from numbat.optimizer.model import OptimizerInputs
from numbat.planner import CycleData, InputsStale, Planner
from numbat.timegrid import TimeGrid

ADELAIDE = ZoneInfo("Australia/Adelaide")
# Mid-fixture-era: after the general price interval start (11:35Z), within
# staleness windows of both price sensors.
NOW = datetime(2026, 7, 15, 11, 36, 30, tzinfo=UTC)

SETTINGS_DICT = {
    "entities": {
        "buy_price": "sensor.amber_express_general_price",
        "sell_price": "sensor.amber_express_feed_in_price",
        "price_spike": "binary_sensor.amber_express_price_spike",
        "pv_forecast_today": "sensor.home_energy_production_today",
        "pv_forecast_tomorrow": "sensor.home_energy_production_tomorrow",
        "battery_soc": "sensor.battery_level",
        "battery_power": "sensor.battery_power",
        "weather": "weather.henley_beach_hourly",
    },
    "battery": {"capacity_kwh": 12.8, "max_charge_kw": 5.0, "max_discharge_kw": 5.0},
    "grid": {"import_limit_kw": 15.0, "export_limit_kw": 5.0},
}


def make_settings(**overrides) -> Settings:
    base = {**SETTINGS_DICT, **overrides}
    return Settings.model_validate(base)


def add_battery_states(
    fake: FakeHa,
    soc: str = "72.5",
    power: str = "-1200",
    ts: str = "2026-07-15T11:35:00+00:00",
) -> None:
    fake.states["sensor.battery_level"] = {
        "entity_id": "sensor.battery_level",
        "state": soc,
        "attributes": {"unit_of_measurement": "%"},
        "last_updated": ts,
    }
    fake.states["sensor.battery_power"] = {
        "entity_id": "sensor.battery_power",
        "state": power,
        "attributes": {"unit_of_measurement": "W"},
        "last_updated": ts,
    }


def full_fake_ha() -> FakeHa:
    fake = FakeHa()
    for name in (
        "amber_express_feed_in_price",
        "amber_express_general_price",
        "amber_express_price_spike",
        "solar_production_today",
        "solar_production_tomorrow",
        "weather_henley_beach_hourly",
    ):
        fake.add_fixture(name)
    add_battery_states(fake)
    fake.service_responses[("weather", "get_forecasts")] = {
        "weather.henley_beach_hourly": {
            "forecast": [
                {"datetime": "2026-07-15T21:00:00+09:30", "temperature": 6.5},
                {"datetime": "2026-07-16T00:00:00+09:30", "temperature": 5.0},
                {"datetime": "2026-07-16T09:00:00+09:30", "temperature": 12.5},
            ]
        }
    }
    return fake


class FixedLoadForecaster:
    """Constant-load stand-in so planner tests keep deterministic economics."""

    status = "learned"
    details = {}

    def __init__(self, kw: float = 0.5):
        self._kw = kw

    async def refresh(self, now):
        return None

    def forecast(self, grid, temps_c):
        return np.full(len(grid), self._kw)


def make_planner(client, settings: Settings) -> Planner:
    return Planner(
        settings,
        prices=AmberExpressAdapter(client, settings.entities),
        solar=OpenMeteoSolarAdapter(client, settings.entities),
        battery=SungrowAdapter(client, settings.entities, settings.battery),
        weather=WeatherAdapter(client, settings.entities),
        tz=ADELAIDE,
        load_forecaster=FixedLoadForecaster(),
    )


async def test_full_cycle_against_fixtures():
    settings = make_settings()
    fake = full_fake_ha()
    async with fake_ha_client(fake) as client:
        planner = make_planner(client, settings)
        plan = await planner.run_cycle(NOW)

    assert plan.solver_status in ("optimal", "optimal_inaccurate")
    assert plan.intervals[0].start == NOW
    assert plan.intervals[-1].end == NOW + timedelta(hours=36)
    # Step 0 uses the live sensor states, not the forecast attribute
    assert plan.intervals[0].buy == pytest.approx(0.44)
    # profile mode: hourly baseline only (temperature sensitivity is
    # history+outdoor_temp's job)
    assert plan.intervals[0].load_kw == pytest.approx(0.5)
    # Battery at 72.5%: tomorrow evening's high prices should provoke export at
    # some point in the horizon
    assert any(iv.action == Action.DISCHARGE for iv in plan.intervals)


async def test_absurd_load_forecast_is_clamped_not_infeasible():
    """Live failure 2026-07-16: a mislabeled load sensor produced a ~250 kW
    forecast and the MILP came back infeasible. Bad load data must clamp to
    the import limit and still solve."""
    settings = make_settings()
    fake = full_fake_ha()
    async with fake_ha_client(fake) as client:
        planner = make_planner(client, settings)
        planner._load_forecaster = FixedLoadForecaster(250.0)
        plan = await planner.run_cycle(NOW)
    assert plan.solver_status in ("optimal", "optimal_inaccurate")
    assert plan.intervals[0].load_kw == pytest.approx(settings.grid.import_limit_kw)


async def test_old_battery_report_is_tolerated():
    """mkaiser battery sensors only report on value change, so an old
    last_reported must NOT abort the cycle (idle battery == constant SoC).
    Unavailable battery sensors are still fatal (adapter raises)."""
    settings = make_settings()
    fake = full_fake_ha()
    add_battery_states(fake, ts="2026-07-15T09:00:00+00:00")  # 2.5h old
    async with fake_ha_client(fake) as client:
        planner = make_planner(client, settings)
        data = await planner.gather(NOW)
    assert data.battery.soc_frac == pytest.approx(0.725)


async def test_stale_prices_still_fatal():
    settings = make_settings()
    fake = full_fake_ha()
    async with fake_ha_client(fake) as client:
        planner = make_planner(client, settings)
        late = NOW + timedelta(hours=2)  # price sensors reported 11:35
        with pytest.raises(InputsStale, match="prices"):
            await planner.gather(late)


async def test_unchanged_but_reported_battery_is_fresh():
    """HA only bumps last_updated when the VALUE changes; a battery sitting at
    a constant SoC must not be treated as stale while last_reported is fresh.
    (Regression: live loop wrongly went degraded after ~10 min of flat SoC.)"""
    settings = make_settings()
    fake = full_fake_ha()
    add_battery_states(fake, ts="2026-07-15T10:00:00+00:00")  # value unchanged for 1.5h
    for entity in ("sensor.battery_level", "sensor.battery_power"):
        fake.states[entity]["last_reported"] = "2026-07-15T11:36:00+00:00"  # polled 30s ago
    async with fake_ha_client(fake) as client:
        planner = make_planner(client, settings)
        data = await planner.gather(NOW)
    assert data.battery.soc_frac == pytest.approx(0.725)


async def test_spike_reserve_armed_from_forecast():
    settings = make_settings(
        spike={"lookahead_hours": 48, "reserve_kwh": 6.0, "high_price_threshold": 0.6}
    )
    fake = full_fake_ha()
    async with fake_ha_client(fake) as client:
        planner = make_planner(client, settings)
        data = await planner.gather(NOW)
    reserve = data.inputs.reserve_kwh
    assert reserve is not None
    assert reserve.max() == pytest.approx(6.0)
    assert reserve[0] == pytest.approx(6.0)  # held from now...
    assert reserve[-1] == 0.0  # ...released at/after the trigger step


def synthetic_cycle_data(settings: Settings, live_spike: bool = False) -> CycleData:
    T = 12
    start = NOW
    bounds = [start + timedelta(minutes=30 * i) for i in range(1, T)]
    grid = TimeGrid.build(start, bounds, timedelta(hours=6))
    inputs = OptimizerInputs(
        dt_hours=grid.dt_hours,
        buy=np.full(len(grid), 0.30),
        sell=np.full(len(grid), 0.10),
        pv=np.zeros(len(grid)),
        load=np.full(len(grid), 0.5),
        soc0_kwh=6.4,
    )
    series = Series(times=[start], values=[0.30])
    prices = PriceForecast(
        buy=series, sell=series, current_buy=0.30, current_sell=0.10, live_spike=live_spike
    )
    battery = BatteryState(soc_frac=0.5, power_kw=0.0, capacity_kwh=12.8, ts=start)
    return CycleData(grid=grid, inputs=inputs, prices=prices, battery=battery, temps=None)


def previous_plan_with(action: Action) -> Plan:
    iv = PlanInterval(
        start=NOW - timedelta(minutes=5),
        end=NOW + timedelta(minutes=25),
        action=action,
        power_kw=0.0,
        soc_start=6.4,
        soc_end=6.4,
        buy=0.30,
        sell=0.10,
        pv_kw=0.0,
        load_kw=0.5,
        grid_import_kw=0.5,
        grid_export_kw=0.0,
        interval_cost=0.075,
    )
    return Plan(
        intervals=[iv],
        objective_cost=0.0,
        solver_status="optimal",
        solve_ms=1.0,
        computed_at=NOW - timedelta(minutes=5),
    )


def offline_planner(settings: Settings) -> Planner:
    # optimize()/hysteresis don't touch the adapters, so dummies are fine here
    return Planner(
        settings,
        prices=cast(AmberExpressAdapter, None),
        solar=cast(OpenMeteoSolarAdapter, None),
        battery=cast(SungrowAdapter, None),
        weather=cast(WeatherAdapter, None),
        tz=ADELAIDE,
        load_forecaster=FixedLoadForecaster(),
    )


def test_load_serving_discharge_classifies_as_idle():
    """Flat prices, no PV: the battery runs the house, nothing touches the
    grid — that's self-consumption, so the published action is IDLE (the
    inverter's native mode does this load-followingly), not DISCHARGE."""
    settings = make_settings(
        optimizer={"action_switch_threshold_dollars": 0.0, "forecast_haircut": 0.0}
    )
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    plan = planner.optimize(data, NOW)
    step0 = plan.intervals[0]
    assert step0.action == Action.IDLE
    assert step0.power_kw == pytest.approx(-0.5, abs=0.05)  # battery serves the load
    assert step0.grid_export_kw == pytest.approx(0.0, abs=0.01)


def test_hysteresis_keeps_near_degenerate_previous_action():
    """Step-0 buy price marginally below the terminal value makes a grid
    charge worth well under the threshold, so the previous action (idle:
    cheap import serves the load, battery waits) is kept."""
    # pin the hold value so this isolates the hysteresis mechanism from the
    # rebuy anchor (which would otherwise track buy[0] down and make the charge
    # a wash — a separate behavior, covered in test_optimizer).
    settings = make_settings(
        optimizer={
            "action_switch_threshold_dollars": 0.05,
            "forecast_haircut": 0.0,
            "terminal_soc_value": 0.245,
        }
    )
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    data.inputs.buy[0] = 0.23  # just below the 0.245 hold value: charging gains cents
    planner.previous_plan = previous_plan_with(Action.IDLE)
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action == Action.IDLE
    assert "hysteresis" in plan.solver_status


def test_hysteresis_disabled_switches_freely():
    settings = make_settings(
        optimizer={
            "action_switch_threshold_dollars": 0.0,
            "forecast_haircut": 0.0,
            "terminal_soc_value": 0.245,
        }
    )
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    data.inputs.buy[0] = 0.23
    planner.previous_plan = previous_plan_with(Action.IDLE)
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action == Action.CHARGE  # grid charge wins


def test_live_spike_guard_suppresses_grid_charge():
    settings = make_settings(optimizer={"action_switch_threshold_dollars": 0.0})
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings, live_spike=True)
    data.inputs.buy[0] = -0.10  # would normally trigger a grid charge now
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action != Action.CHARGE


def test_spike_reserve_vector_lookahead_and_trigger():
    from numbat.planner import spike_reserve_vector

    dt = np.full(12, 0.5)  # 6h of 30-min steps
    sell = np.full(12, 0.10)
    sell[6] = 5.0  # 3h out, inside 4h lookahead
    reserve = spike_reserve_vector(
        sell, dt, lookahead_hours=4, high_price_threshold=1.0, reserve_kwh=6.0, soc_max_kwh=44.8
    )
    assert reserve is not None
    assert (reserve[:6] == 6.0).all()
    assert (reserve[6:] == 0.0).all()

    sell2 = np.full(12, 0.10)
    sell2[10] = 5.0  # 5h out, beyond lookahead
    assert (
        spike_reserve_vector(
            sell2, dt, lookahead_hours=4, high_price_threshold=1.0, reserve_kwh=6.0,
            soc_max_kwh=44.8,
        )
        is None
    )


def test_fallback_shifts_previous_plan():
    settings = make_settings()
    planner = offline_planner(settings)
    planner.previous_plan = previous_plan_with(Action.IDLE)
    fallback = planner.fallback(NOW)
    assert fallback.solver_status.startswith("stale")
    assert all(iv.end > NOW for iv in fallback.intervals)


def test_daily_soc_target_vector_windowed_across_days():
    from numbat.planner import daily_soc_target_vector
    from numbat.timegrid import TimeGrid

    # 00:00 UTC = 09:30 in Adelaide; 15:00 local = 05:30 UTC. soc[1:] alignment:
    # the step whose END is 05:30 is index 10 (its end is soc[11]). A 0-hour
    # hold keeps just that one step; the target repeats next day (+24h).
    now = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)
    boundaries = [now + timedelta(minutes=30 * i) for i in range(80)]
    grid = TimeGrid.build(now, boundaries, timedelta(hours=36))
    instant = daily_soc_target_vector(
        grid, ADELAIDE, target_soc=1.0, target_time=dt_time(15, 0), hold_hours=0.0,
        capacity_kwh=44.8,
    )
    assert instant is not None and len(instant) == len(grid)  # aligned with soc[1:]
    assert instant[10] == pytest.approx(44.8)  # soc[11] == 05:30 UTC == 15:00 Adelaide
    assert list(np.nonzero(instant)[0]) == [10, 58]  # today + tomorrow, one step each

    # a 2-hour hold widens each day's floor to the step-ends in [15:00, 17:00]
    windowed = daily_soc_target_vector(
        grid, ADELAIDE, target_soc=1.0, target_time=dt_time(15, 0), hold_hours=2.0,
        capacity_kwh=44.8,
    )
    assert windowed is not None
    assert list(np.nonzero(windowed)[0]) == [10, 11, 12, 13, 14, 58, 59, 60, 61, 62]

    # clamp to soc_max so an over-100% target can't bake in a phantom penalty
    clamped = daily_soc_target_vector(
        grid, ADELAIDE, target_soc=1.0, target_time=dt_time(15, 0), hold_hours=0.0,
        capacity_kwh=44.8, soc_max_kwh=40.0,
    )
    assert clamped is not None and clamped[10] == pytest.approx(40.0)

    # a window that has already fully elapsed for today must NOT pin step 0 —
    # only tomorrow's window remains (regression: the arrival fallback used to
    # trivially match step 0 for a past win_start, holding the battery full
    # through the very evening peak it should discharge into).
    later = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)  # 21:30 Adelaide, past 15:00+4h
    b2 = [later + timedelta(minutes=30 * i) for i in range(80)]
    g2 = TimeGrid.build(later, b2, timedelta(hours=36))
    elapsed = daily_soc_target_vector(
        g2, ADELAIDE, target_soc=1.0, target_time=dt_time(15, 0), hold_hours=4.0,
        capacity_kwh=44.8,
    )
    assert elapsed is not None
    assert elapsed[0] == 0.0  # step 0 not pinned
    assert np.count_nonzero(elapsed) == 9  # only tomorrow's 4h window (9 step-ends)

    # disabled
    assert (
        daily_soc_target_vector(
            grid, ADELAIDE, target_soc=0.0, target_time=dt_time(15, 0), hold_hours=4.0,
            capacity_kwh=44.8,
        )
        is None
    )


async def test_load_buffer_scales_the_forecast():
    # load.buffer plans for consistently more than the learned mean; applied
    # before the feasibility clamp and surfaced in load_forecast_info
    fake = full_fake_ha()
    async with fake_ha_client(fake) as client:
        plain = await make_planner(client, make_settings()).gather(NOW)
        buffered = await make_planner(
            client, make_settings(load={"buffer": 0.25})
        ).gather(NOW)
    assert np.allclose(buffered.inputs.load, plain.inputs.load * 1.25)
    assert buffered.load_forecast_info["buffer"] == 0.25
    assert "buffer" not in plain.load_forecast_info


async def test_vacation_mode_flattens_load_until_return():
    # NOW is 2026-07-15T11:36Z; a 30-min-grid horizon runs ~36h ahead.
    # Vacation until 2026-07-16T00:00Z (naive local 09:30 Adelaide): away
    # steps get the flat unbuffered baseline, later steps revert to the
    # learned forecast WITH the buffer.
    fake = full_fake_ha()
    settings = make_settings(
        load={"buffer": 0.2},
        vacation={"enabled": True, "baseline_kw": 0.25, "until": "2026-07-16T09:30:00"},
    )
    async with fake_ha_client(fake) as client:
        data = await make_planner(client, settings).gather(NOW)
    until_utc = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    for step, load in zip(data.grid.steps, data.inputs.load, strict=True):
        if step.start < until_utc:
            assert load == pytest.approx(0.25)  # baseline, no buffer
        else:
            assert load == pytest.approx(0.5 * 1.2)  # learned, buffered
    assert data.vacation == {"baseline_kw": 0.25, "until": "2026-07-16T09:30:00"}


async def test_vacation_mode_expired_or_disabled_is_inert():
    fake = full_fake_ha()
    expired = make_settings(
        vacation={"enabled": True, "baseline_kw": 0.25, "until": "2026-07-01T00:00:00"}
    )
    disabled = make_settings(vacation={"baseline_kw": 0.25})
    async with fake_ha_client(fake) as client:
        for settings in (expired, disabled):
            data = await make_planner(client, settings).gather(NOW)
            assert np.allclose(data.inputs.load, 0.5)  # learned forecast
            assert data.vacation is None


async def test_vacation_mode_open_ended_covers_whole_horizon():
    fake = full_fake_ha()
    settings = make_settings(vacation={"enabled": True, "baseline_kw": 0.3})
    async with fake_ha_client(fake) as client:
        data = await make_planner(client, settings).gather(NOW)
    assert np.allclose(data.inputs.load, 0.3)
    assert data.vacation == {"baseline_kw": 0.3, "until": None}


async def test_daily_target_wired_into_inputs():
    settings = make_settings(
        battery={
            "capacity_kwh": 12.8,
            "max_charge_kw": 5.0,
            "max_discharge_kw": 5.0,
            "daily_target_soc": 1.0,
        }
    )
    fake = full_fake_ha()
    async with fake_ha_client(fake) as client:
        planner = make_planner(client, settings)
        data = await planner.gather(NOW)
    target = data.inputs.soc_target_kwh
    assert target is not None
    assert float(np.max(target)) == pytest.approx(12.8)
