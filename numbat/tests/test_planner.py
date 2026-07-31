from dataclasses import replace
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


async def test_cycle_without_weather_entity_skips_the_forecast_call():
    # entities.weather is optional: no weather.get_forecasts call is made
    # (no per-cycle warning spam) and planning proceeds without temps.
    settings = make_settings(entities={**SETTINGS_DICT["entities"], "weather": ""})
    fake = full_fake_ha()
    async with fake_ha_client(fake) as client:
        plan = await make_planner(client, settings).run_cycle(NOW)
    assert plan.solver_status in ("optimal", "optimal_inaccurate")
    assert not any(d == "weather" for d, _s, _data in fake.service_calls)


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


async def test_gather_wires_the_spike_reserve_sales_floor():
    """Enabled reserve → a per-step sales floor built from the SOLVE (trimmed)
    series: released exactly where that series clears the threshold."""
    fake = full_fake_ha()

    def spike_settings(threshold: float) -> Settings:
        return make_settings(
            optimizer={"forecast_haircut": 0.5},
            spike={"reserve_soc": 0.5, "high_price_threshold": threshold},
        )

    async with fake_ha_client(fake) as client:
        # threshold nothing clears: floored everywhere at 50% of capacity
        held = await make_planner(client, spike_settings(999.0)).gather(NOW)
        floor = held.inputs.sell_floor_kwh
        assert floor is not None
        assert np.all(floor == pytest.approx(0.5 * 12.8))
        # a threshold between the trimmed and raw forecast peaks: wired to the
        # trimmed series, nothing releases; wired to raw, the peak step would
        trimmed_peak = float(held.inputs.sell[1:].max())
        raw_peak = float(held.sell_raw[1:].max())
        assert trimmed_peak < raw_peak
        mid = await make_planner(
            client, spike_settings((trimmed_peak + raw_peak) / 2)
        ).gather(NOW)
        assert np.all(mid.inputs.sell_floor_kwh == pytest.approx(0.5 * 12.8))
        # a threshold just under the trimmed peak: that forecast step (and any
        # like it) releases IN-PLAN; step 0 (low confirmed price) stays floored
        antic = await make_planner(
            client, spike_settings(trimmed_peak - 0.001)
        ).gather(NOW)
        floor = antic.inputs.sell_floor_kwh
        assert floor is not None
        assert floor[0] == pytest.approx(0.5 * 12.8)  # confirmed price low
        released_mask = np.isclose(floor, 0.10 * 12.8)  # export reserve default
        expected = antic.inputs.sell > trimmed_peak - 0.001
        assert np.array_equal(released_mask, expected)
        assert expected[1:].any()
        # disabled (or not above the export reserve): no floor machinery
        off = await make_planner(client, make_settings()).gather(NOW)
        assert off.inputs.sell_floor_kwh is None


async def test_gather_wires_the_spike_discharge_caps():
    """The step-0 raised cap comes from the DERIVED live spike (current sell
    above the threshold); later steps from the solve series."""
    fake = full_fake_ha()

    def cap_settings(threshold: float) -> Settings:
        return make_settings(spike={"discharge_kw": 15.0, "high_price_threshold": threshold})

    async with fake_ha_client(fake) as client:
        # current 0.1585 below a 0.50 threshold: everyday 5 kW cap at step 0,
        # raised exactly where the forecast clears the threshold
        data = await make_planner(client, cap_settings(0.50)).gather(NOW)
        caps = data.inputs.max_discharge_kw_step
        assert caps is not None
        assert caps[0] == 5.0
        raised = caps[1:] == 15.0
        assert np.array_equal(raised, data.inputs.sell[1:] > 0.50)
        assert raised.any()
        # a threshold under the live price: the step-0 cap is raised too
        live = await make_planner(client, cap_settings(0.10)).gather(NOW)
        assert live.inputs.max_discharge_kw_step[0] == 15.0


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
    # A live spike is purely a price condition: current sell above
    # spike.high_price_threshold (default $1). inputs.sell[0] deliberately
    # stays 0.10 even then — gather would never produce that (it sets
    # sell[0] = current_sell), but it keeps the optimizer wanting the cheap
    # step-0 action the guard tests need to suppress.
    prices = PriceForecast(
        buy=series, sell=series, current_buy=0.30, current_sell=1.50 if live_spike else 0.10
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
            # the default import toll would kill this deliberately thin
            # charge margin — this test is about hysteresis, not reluctance
            "import_penalty_per_kwh": 0.0,
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
            "import_penalty_per_kwh": 0.0,  # keep the thin charge margin alive
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


def test_sell_floor_vector_release_semantics():
    from numbat.planner import sell_floor_vector

    kw = dict(reserve_kwh=9.6, export_reserve_kwh=3.2, high_price_threshold=1.0)
    # nothing clears the threshold: uniformly floored at the reserve
    sell = np.full(12, 0.90)
    floor = sell_floor_vector(sell, **kw)
    assert floor is not None and np.all(floor == 9.6)
    # confirmed step 0 clears it: released to the export reserve
    sell[0] = 5.60
    floor = sell_floor_vector(sell, **kw)
    assert floor is not None
    assert floor[0] == 3.2 and np.all(floor[1:] == 9.6)
    # a FORECAST step above the threshold releases in-plan too (anticipation;
    # execution still needs the confirmed price when that interval arrives)
    sell = np.full(12, 0.90)
    sell[5] = 1.88
    floor = sell_floor_vector(sell, **kw)
    assert floor is not None
    assert floor[5] == 3.2 and floor[0] == 9.6
    assert np.all(np.delete(floor, 5) == 9.6)
    # inert unless the reserve sits above the export reserve
    assert sell_floor_vector(sell, reserve_kwh=3.0, export_reserve_kwh=3.2,
                             high_price_threshold=1.0) is None


async def test_gather_haircuts_solve_prices_and_keeps_raw_for_display():
    """The wiring, not the maths: gather must hand the solver the trimmed
    series AND stash the raw one for the published plan. (A dropped
    sell_raw= or a raw inputs.sell would silently pass every other test —
    they run with the haircut off.)"""
    settings = make_settings(optimizer={"forecast_haircut": 0.5})
    fake = full_fake_ha()
    async with fake_ha_client(fake) as client:
        data = await make_planner(client, settings).gather(NOW)
    assert data.sell_raw is not None
    assert data.inputs.sell[0] == data.sell_raw[0]  # live price exempt
    median = float(np.median(data.sell_raw))
    above = data.sell_raw[1:] > median
    assert above.any()  # fixture sanity: something to trim
    assert np.all(data.inputs.sell[1:][above] < data.sell_raw[1:][above])
    assert np.array_equal(data.inputs.sell[1:][~above], data.sell_raw[1:][~above])


def test_haircut_trims_forecast_above_median_but_never_step0():
    """The haircut is a flat trim on every FORECAST interval's excess above
    the median; the live step-0 price is confirmed and never cut, biasing
    the plan toward selling at a good confirmed price over holding for a
    forecast better one."""
    settings = make_settings(optimizer={"forecast_haircut": 0.10})
    planner = offline_planner(settings)
    sell = np.array([0.90, 0.90, 0.30, 0.20, 0.10])  # median 0.30
    out = planner._haircut_sell(sell)
    assert out[0] == 0.90  # step 0: confirmed, untouched
    assert out[1] == pytest.approx(0.30 + 0.60 * 0.9)  # forecast spike trimmed
    assert out[2] == 0.30  # at the median: no excess to trim
    assert out[3] == 0.20  # below median: untouched
    assert out[4] == 0.10


def test_haircut_off_is_identity():
    settings = make_settings(optimizer={"forecast_haircut": 0.0})
    planner = offline_planner(settings)
    sell = np.array([0.90, 0.80, 0.10])
    assert np.array_equal(planner._haircut_sell(sell), sell)


def test_plan_reports_raw_prices_not_haircut_ones():
    """The haircut shapes the solve only — the published plan (dashboard
    chart, interval costs) must quote the real forecast prices."""
    settings = make_settings(optimizer={"forecast_haircut": 0.50})
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    raw = data.inputs.sell.copy()
    raw[3] = 0.80  # a forecast blip the haircut would halve toward the median
    data = replace(data, sell_raw=raw, inputs=replace(data.inputs, sell=planner._haircut_sell(raw)))
    assert data.inputs.sell[3] < 0.80  # sanity: the solve really saw a trim
    plan = planner.optimize(data, NOW)
    assert plan.intervals[3].sell == pytest.approx(0.80)


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
