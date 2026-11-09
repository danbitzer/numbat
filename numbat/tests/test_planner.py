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
from numbat.optimizer.model import GridParams, OptimizerInputs
from numbat.planner import (
    CycleData,
    InputsStale,
    Planner,
    battery_params,
    discharge_request_kw,
)
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
        mid = await make_planner(client, spike_settings((trimmed_peak + raw_peak) / 2)).gather(NOW)
        assert np.all(mid.inputs.sell_floor_kwh == pytest.approx(0.5 * 12.8))
        # a threshold just under the trimmed peak: that forecast step (and any
        # like it) releases IN-PLAN; step 0 (low confirmed price) stays floored
        antic = await make_planner(client, spike_settings(trimmed_peak - 0.001)).gather(NOW)
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
    # optimize() doesn't touch the adapters, so dummies are fine here
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
    settings = make_settings(optimizer={"forecast_haircut": 0.0})
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    plan = planner.optimize(data, NOW)
    step0 = plan.intervals[0]
    assert step0.action == Action.IDLE
    assert step0.power_kw == pytest.approx(-0.5, abs=0.05)  # battery serves the load
    assert step0.grid_export_kw == pytest.approx(0.0, abs=0.01)


def test_live_spike_guard_suppresses_grid_charge():
    settings = make_settings()
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings, live_spike=True)
    data.inputs.buy[0] = -0.10  # would normally trigger a grid charge now
    plan = planner.optimize(data, NOW)
    step0 = plan.intervals[0]
    assert step0.action != Action.CHARGE
    # the suppressed charge no longer comes in over the meter: the numbers
    # (and the flow the dashboard derives from them) match the new action
    # re-stated as what idle actuates during a spike: self-consumption, the
    # battery covering the house (load 0.5 kW, no solar at night)
    assert step0.power_kw == pytest.approx(-0.5)
    assert step0.grid_import_kw == 0.0
    assert step0.soc_end < step0.soc_start
    assert step0.interval_cost == 0.0
    assert step0.flow == "running_on_battery"


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
    assert (
        sell_floor_vector(sell, reserve_kwh=3.0, export_reserve_kwh=3.2, high_price_threshold=1.0)
        is None
    )


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


def test_fallback_carries_the_curtail_flag():
    # like live_spike: the published attribute must stay truthful while a
    # stale plan is being reused
    settings = make_settings()
    planner = offline_planner(settings)
    prev = previous_plan_with(Action.IDLE)
    prev.curtail_export = True
    planner.previous_plan = prev
    assert planner.fallback(NOW).curtail_export is True


def test_fallback_drops_curtail_when_the_surviving_step_exports():
    # a carried cap must not throttle a step that planned to sell (evening
    # export after a negative midday, solver failing at the rollover)
    settings = make_settings()
    planner = offline_planner(settings)
    prev = previous_plan_with(Action.DISCHARGE)
    prev.curtail_export = True
    prev.intervals[0].grid_export_kw = 4.0
    planner.previous_plan = prev
    assert planner.fallback(NOW).curtail_export is False


def test_plan_curtail_flag_follows_negative_feed_in():
    """The 2026-09-10 gap: 'charge' at negative buy AND feed-in must ALSO say
    withhold export, or the actuator lifts the cap and the PV surplus pays
    to export. The flag is orthogonal to the action."""
    settings = make_settings()
    planner = offline_planner(settings)
    # negative buy and feed-in: the plan grid-charges with zero export
    data = synthetic_cycle_data(settings)
    data.inputs.buy[:] = -0.02
    data.inputs.sell[:] = -0.12
    data.prices.current_sell = -0.12
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action == Action.CHARGE
    assert plan.curtail_export is True
    # positive feed-in: never curtail, whatever the plan exports
    calm = synthetic_cycle_data(settings)
    calm_plan = planner.optimize(calm, NOW)
    assert calm_plan.curtail_export is False
    # exactly zero is not negative: exporting at $0 is harmless, leave the
    # limit open for a positive PV surprise
    zero = synthetic_cycle_data(settings)
    zero.prices.current_sell = 0.0
    assert planner.optimize(zero, NOW).curtail_export is False


def test_hold_plus_curtail_is_the_negative_price_combination():
    """PV spill at negative buy AND feed-in with a full battery: the action
    is HOLD (battery fenced, grid serves the house — self-consumption would
    discharge into the load) and the curtail flag rides it (export capped).
    This is the 2026-09-10 persistent-negative-day shape."""
    settings = make_settings()
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    data.inputs.pv[:] = 2.0
    data.inputs.buy[:] = -0.02
    data.inputs.sell[:] = -0.05
    data.prices.current_sell = -0.05
    data = replace(data, inputs=replace(data.inputs, soc0_kwh=12.8))
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action == Action.HOLD
    assert plan.curtail_export is True


def test_pv_off_rides_hold_at_negative_buy():
    """Negative buy AND feed-in with a full battery: the plan serves the
    house from the grid (paid to) and uses none of its PV — that intent
    used to be unactuatable (PV serves the house first on every hybrid);
    with a PV-off actuator it is published as `pv_off`, riding HOLD."""
    settings = make_settings(grid=PV_OFF_GRID)
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    data.inputs.pv[:] = 2.0
    data.inputs.buy[:] = -0.02
    data.inputs.sell[:] = -0.05
    data.prices.current_buy = -0.02
    data.prices.current_sell = -0.05
    data = replace(data, inputs=replace(data.inputs, soc0_kwh=12.8))
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action == Action.HOLD
    assert plan.intervals[0].pv_used_kw < 0.01
    assert plan.curtail_export is True
    assert plan.pv_off is True


def test_pv_off_rides_charge_for_true_grid_charging():
    """Room in the battery at negative buy: the plan grid-charges with PV
    off — CHARGE + pv_off is the combination that finally draws the charge
    from the grid instead of throttled PV."""
    settings = make_settings(grid=PV_OFF_GRID)
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    data.inputs.pv[:] = 2.0
    data.inputs.buy[:] = -0.02
    data.inputs.sell[:] = -0.12
    data.prices.current_buy = -0.02
    data.prices.current_sell = -0.12
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action == Action.CHARGE
    assert plan.intervals[0].pv_used_kw < 0.01
    assert plan.pv_off is True


# grid.pv_off_enabled is opt-in (default off): these tests exercise the flag
PV_OFF_GRID = {"import_limit_kw": 15.0, "export_limit_kw": 5.0, "pv_off_enabled": True}


def _negative_buy_pv_day(settings, live_buy: float, estimate: bool = False):
    # the horizon says negative buy (so the plan uses no PV) with a full
    # battery; the LIVE price is whatever the test wants to probe
    data = synthetic_cycle_data(settings)
    data.inputs.pv[:] = 2.0
    data.inputs.buy[:] = -0.02
    data.inputs.sell[:] = -0.05
    data.prices.current_buy = live_buy
    data.prices.current_sell = -0.05
    data.prices.current_estimate = estimate
    return replace(data, inputs=replace(data.inputs, soc0_kwh=12.8))


def test_pv_off_is_gated_on_the_live_buy_price():
    """pv_used = 0 alone is not enough: a solver tie around buy ≈ 0 must not
    flip a 40 s-recovery actuator, so the flag needs a NEGATIVE live buy —
    a cent below zero to switch on, anything below zero to stay on (each
    clear costs ~45 s of PV; a 5-minute site's price can hover at zero).
    And nothing to withhold at night (no PV) means no flag either."""
    settings = make_settings(grid=PV_OFF_GRID)
    planner = offline_planner(settings)
    # positive, zero, and negative-but-shallow live prices: no entry
    for live_buy in (0.30, 0.0, -0.005):
        plan = planner.optimize(_negative_buy_pv_day(settings, live_buy), NOW)
        assert plan.intervals[0].pv_used_kw < 0.01
        assert plan.pv_off is False
    # deep enough: on
    on = planner.optimize(_negative_buy_pv_day(settings, -0.02), NOW)
    assert on.pv_off is True
    # once on (the cycle records the plan), a shallow negative keeps it on;
    # zero switches it off
    planner.previous_plan = on
    still = planner.optimize(_negative_buy_pv_day(settings, -0.005), NOW)
    assert still.pv_off is True
    planner.previous_plan = still
    assert planner.optimize(_negative_buy_pv_day(settings, 0.0), NOW).pv_off is False
    # negative buy but no PV to withhold (night)
    night = synthetic_cycle_data(settings)
    night.inputs.buy[:] = -0.02
    night.prices.current_buy = -0.02
    assert planner.optimize(night, NOW).pv_off is False
    # positive buy: PV serves the house, never withheld
    calm = synthetic_cycle_data(settings)
    calm.inputs.pv[:] = 2.0
    assert planner.optimize(calm, NOW).pv_off is False


def test_pv_off_holds_its_state_while_the_live_price_is_an_estimate():
    """Amber's first seconds of an interval are an estimate that the confirmed
    price can contradict; the confirmed re-solve follows within seconds, so
    an estimate neither switches PV off nor back on."""
    settings = make_settings(grid=PV_OFF_GRID)
    planner = offline_planner(settings)
    # off, deep negative estimate: stays off until confirmed
    est = _negative_buy_pv_day(settings, -0.13, estimate=True)
    assert planner.optimize(est, NOW).pv_off is False
    on = planner.optimize(_negative_buy_pv_day(settings, -0.13), NOW)
    assert on.pv_off is True
    # on, positive estimate: stays on until confirmed
    planner.previous_plan = on
    est = _negative_buy_pv_day(settings, 0.30, estimate=True)
    held = planner.optimize(est, NOW)
    assert held.pv_off is True
    planner.previous_plan = held
    assert planner.optimize(_negative_buy_pv_day(settings, 0.30), NOW).pv_off is False


def test_fallback_is_annotated_and_its_explanation_reconciles():
    # the stale plan carries flows per interval, the published flags in its
    # levers (the dashboard's reconciliation line reads them) and the flow
    # block — the same shape as a fresh plan
    settings = make_settings()
    planner = offline_planner(settings)
    prev = previous_plan_with(Action.HOLD)
    prev.curtail_export = True
    prev.intervals[0].grid_import_kw = 1.0
    planner.previous_plan = prev
    fb = planner.fallback(NOW)
    assert fb.intervals[0].flow == "running_on_grid"
    assert fb.explanation["levers"] == {"live_spike": False, "curtail": True, "pv_off": False}
    assert fb.explanation["flow"]["key"] == "running_on_grid"
    assert fb.explanation["stale"] is True


def test_fallback_carries_pv_off_only_while_the_step_plans_no_pv():
    # like curtail: the live price is unknowable in a fallback, so the
    # carried flag is kept only while the surviving step itself uses no PV
    # AND its own forecast buy is still negative — a solver outage across
    # the rollover to a paid interval must not keep PV off
    settings = make_settings(grid=PV_OFF_GRID)
    planner = offline_planner(settings)
    prev = previous_plan_with(Action.HOLD)
    prev.pv_off = True
    prev.intervals[0].pv_kw = 2.0
    prev.intervals[0].pv_used_kw = 0.0
    prev.intervals[0].buy = -0.02
    planner.previous_plan = prev
    assert planner.fallback(NOW).pv_off is True
    prev.intervals[0].buy = 0.30
    assert planner.fallback(NOW).pv_off is False
    prev.intervals[0].buy = -0.02
    prev.intervals[0].pv_used_kw = 0.5
    assert planner.fallback(NOW).pv_off is False
    prev.intervals[0].pv_used_kw = 0.0
    prev.pv_off = False
    assert planner.fallback(NOW).pv_off is False


def test_no_charge_keeps_the_room_for_a_paid_grid_fill():
    """The 2026-09-10 08:30 replay: solar surplus at negative feed-in with
    room in the battery, and a deep negative BUY price a few hours out.
    The plan spills the solar now to be paid to refill from the grid
    later. Publishing `curtail` would actuate as self-consumption + cap —
    the inverter would fill the battery from solar and forfeit the paid
    fill — so the action must be `no_charge` (cap on the attribute) and
    the dashboard says the battery fills from the grid later."""
    settings = make_settings()
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    data.inputs.pv[:] = 4.0
    data.inputs.sell[:] = -0.05
    data.inputs.buy[:] = 0.09
    data.inputs.buy[6:] = -0.15  # paid to import from 3 h out
    data.prices.current_sell = -0.05
    data = replace(data, inputs=replace(data.inputs, soc0_kwh=9.6))  # 75%
    plan = planner.optimize(data, NOW)
    s0 = plan.intervals[0]
    assert s0.action == Action.NO_CHARGE
    assert s0.pv_spill_kw > 3.0
    assert plan.curtail_export is True
    assert s0.flow == "holding_back_solar"
    fl = plan.explanation["flow"]
    assert fl["next_fill_source"] == "grid"
    assert any(iv.action == Action.CHARGE for iv in plan.intervals[6:])


def test_a_fence_never_outlives_its_reason():
    """2026-11-09: a hold left over from a 5-minute price blip stuck for two
    hours of 44-54c prices because the action-switch hysteresis kept the
    previous label. With hysteresis gone, each cycle publishes what the
    plan wants now: the battery serves the house."""
    settings = make_settings()
    planner = offline_planner(settings)
    planner.previous_plan = previous_plan_with(Action.HOLD)
    data = synthetic_cycle_data(settings)
    data.inputs.buy[:] = 0.48
    data.inputs.load[:] = 0.15
    data.prices.current_buy = 0.48
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action == Action.IDLE
    assert plan.intervals[0].power_kw < -0.1
    assert plan.solver_status == "optimal"


ARBITRATE_GRID = {"import_limit_kw": 15.0, "export_limit_kw": 5.0, "inverter_limits_export": True}


def _spike_plan(
    settings,
    *,
    soc0_kwh: float = 6.4,
    pv_kw: float = 3.0,
    sell0: float = 2.0,
    buy_rest: float = 0.30,
    sell_later: dict[int, float] | None = None,
    first_minutes: int | None = None,
):
    """Optimize a high feed-in at step 0 with solar forecast, through the
    planner's own floor and cap vectors (as gather would build them)."""
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    if first_minutes is not None:
        bounds = [NOW + timedelta(minutes=first_minutes)] + [
            NOW + timedelta(minutes=first_minutes + 30 * i) for i in range(1, 12)
        ]
        grid = TimeGrid.build(NOW, bounds, timedelta(hours=6))
        n = len(grid)
        inputs = replace(
            data.inputs,
            dt_hours=grid.dt_hours,
            buy=np.full(n, 0.30),
            sell=np.full(n, 0.10),
            pv=np.zeros(n),
            load=np.full(n, 0.5),
        )
        data = replace(data, grid=grid, inputs=inputs)
    inputs = data.inputs
    inputs.pv[:] = pv_kw
    inputs.buy[:] = buy_rest
    # a spike lifts both prices (the model keeps sell below buy)
    inputs.buy[0] = max(buy_rest, sell0 + 0.4)
    inputs.sell[0] = sell0
    for i, price in (sell_later or {}).items():
        inputs.sell[i] = price
        inputs.buy[i] = max(buy_rest, price + 0.4)
    prices = PriceForecast(
        buy=Series(times=[NOW], values=[float(inputs.buy[0])]),
        sell=Series(times=[NOW], values=[sell0]),
        current_buy=float(inputs.buy[0]),
        current_sell=sell0,
    )
    inputs = replace(
        inputs,
        soc0_kwh=soc0_kwh,
        sell_floor_kwh=planner._sell_floor(inputs.sell),
        max_discharge_kw_step=planner._discharge_caps(inputs.sell, planner._live_spike(prices)),
    )
    return planner.optimize(replace(data, inputs=inputs, prices=prices), NOW)


def test_export_limited_discharge_requests_the_full_cap():
    """2026-11-09: a $3.69 spike under cloud. The plan sized the discharge
    around 9 kW of FORECAST solar; 3 kW turned up and ~6 kW of export
    headroom went unused. With the inverter enforcing the export limit
    itself, the request is the step's full discharge cap and the inverter
    trims the battery against the solar that actually arrives — the plan's
    own numbers are untouched."""
    plan = _spike_plan(make_settings(grid=ARBITRATE_GRID))
    s0 = plan.intervals[0]
    assert s0.action == Action.DISCHARGE
    assert s0.grid_export_kw == pytest.approx(5.0, abs=0.01)  # pinned at the limit
    assert s0.power_kw == pytest.approx(-2.5, abs=0.05)  # 5 - (3 solar - 0.5 load)
    assert plan.setpoint_kw == pytest.approx(-5.0)  # the battery's full cap


def test_discharge_request_is_opt_in():
    # off by default: an inverter that doesn't enforce the limit would over-export
    off = make_settings()
    assert off.grid.inverter_limits_export is False
    plan = _spike_plan(off)
    assert plan.intervals[0].action == Action.DISCHARGE
    assert plan.setpoint_kw is None
    # on, nothing to discharge: nothing to request
    on = make_settings(grid=ARBITRATE_GRID)
    assert offline_planner(on).optimize(synthetic_cycle_data(on), NOW).setpoint_kw is None


def test_discharge_request_only_when_the_export_limit_cut_the_plan_short():
    """A discharge the plan sized for any other reason (price, energy it is
    keeping for the house) is left alone, however much the battery could
    give: only an export pinned at the limit means the plan wanted more."""
    settings = make_settings(grid=ARBITRATE_GRID)
    inputs = synthetic_cycle_data(settings).inputs
    battery = battery_params(settings)
    grid = GridParams(import_limit_kw=15.0, export_limit_kw=5.0)
    step0 = previous_plan_with(Action.DISCHARGE).intervals[0]
    step0.power_kw = -2.5
    step0.grid_export_kw = 2.0  # 2.5 kW out of the battery, export well under 5 kW
    assert discharge_request_kw([step0], inputs, battery, grid) is None
    step0.grid_export_kw = 5.0  # the same discharge, pinned at the export limit
    assert discharge_request_kw([step0], inputs, battery, grid) == pytest.approx(5.0)
    # and only for a forced discharge: self-consumption at the limit is solar
    idle = previous_plan_with(Action.IDLE).intervals[0]
    idle.power_kw = -2.5
    idle.grid_export_kw = 5.0
    assert discharge_request_kw([idle], inputs, battery, grid) is None


def test_discharge_request_never_sells_through_the_floor():
    # 3.0 kWh with a 1.28 kWh export reserve: one 30-min step can give
    # (3.0 - 1.28) * 0.95 / 0.5 h = 3.27 kW, less than the 5 kW cap
    plan = _spike_plan(make_settings(grid=ARBITRATE_GRID), soc0_kwh=3.0)
    assert plan.intervals[0].action == Action.DISCHARGE
    assert plan.setpoint_kw == pytest.approx(-(3.0 - 1.28) * 0.95 / 0.5, abs=0.02)
    # an unreleased spike reserve (50 % = 6.4 kWh, 80c is below the $1
    # release) is the floor: (8.0 - 6.4) * 0.95 / 0.5 = 3.04 kW
    held = make_settings(grid=ARBITRATE_GRID, spike={"reserve_soc": 0.5})
    assert _spike_plan(held, soc0_kwh=8.0, sell0=0.80).setpoint_kw == pytest.approx(-3.04, abs=0.02)
    # released by a live spike: the floor drops to the export reserve
    assert _spike_plan(held, soc0_kwh=8.0, sell0=2.0).setpoint_kw == pytest.approx(-5.0)


def test_discharge_request_uses_the_spike_cap():
    # spike.discharge_kw lifts the step's cap above the everyday 5 kW
    settings = make_settings(
        grid={**ARBITRATE_GRID, "export_limit_kw": 10.0}, spike={"discharge_kw": 10.0}
    )
    plan = _spike_plan(settings, soc0_kwh=10.0, pv_kw=6.0)
    assert plan.intervals[0].power_kw == pytest.approx(-4.5, abs=0.05)  # 10 - (6 - 0.5)
    assert plan.setpoint_kw == pytest.approx(-10.0)


def test_discharge_request_assumes_at_least_a_five_minute_step():
    # after a price event step 0 can be a minute long: sizing the energy
    # bound on that would allow the full cap from a nearly empty battery.
    # (1.6 - 1.28) * 0.95 / (5/60 h) = 3.65 kW, not 5
    plan = _spike_plan(make_settings(grid=ARBITRATE_GRID), soc0_kwh=1.6, first_minutes=1)
    assert plan.intervals[0].action == Action.DISCHARGE
    assert plan.setpoint_kw == pytest.approx(-(1.6 - 1.28) * 0.95 / (5 / 60), abs=0.03)


def test_discharge_request_does_not_wait_for_a_predicted_better_price():
    """$1 now, $3 forecast in half an hour: the plan keeps some energy for
    the $3 interval, but the request still asks for the full cap now. Events
    worth selling everything into are short and a predicted peak is only a
    prediction — a good confirmed price in hand wins (Dan's call,
    2026-11-10; the trade is documented on discharge_request_kw)."""
    plan = _spike_plan(
        make_settings(grid=ARBITRATE_GRID), soc0_kwh=5.0, sell0=1.0, sell_later={1: 3.0}
    )
    assert plan.intervals[0].action == Action.DISCHARGE
    assert plan.intervals[1].action == Action.DISCHARGE  # a dearer sale is planned
    assert plan.setpoint_kw == pytest.approx(-5.0)


def test_pv_off_is_opt_in():
    """grid.pv_off_enabled defaults off: the same negative-buy hold publishes
    no PV-off flag and marks no interval, while the plan is unchanged."""
    settings = make_settings()
    assert settings.grid.pv_off_enabled is False
    planner = offline_planner(settings)
    plan = planner.optimize(_negative_buy_pv_day(settings, -0.13), NOW)
    assert plan.intervals[0].action == Action.HOLD
    assert plan.intervals[0].pv_used_kw < 0.01
    assert plan.pv_off is False
    assert not any(iv.pv_off for iv in plan.intervals)
    assert plan.explanation["flow"]["pv_off"] is False


def test_curtail_action_survives_where_grid_import_is_not_wanted():
    """PV spill at negative feed-in but POSITIVE buy: PV serves the house
    (importing would cost money), the rest is spilled, the full battery
    just sits — the classic standalone CURTAIL action, still carrying the
    flag. No grid import means there is nothing for HOLD to guard against."""
    settings = make_settings()
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    data.inputs.pv[:] = 2.0
    data.inputs.sell[:] = -0.05
    data.prices.current_sell = -0.05
    data = replace(data, inputs=replace(data.inputs, soc0_kwh=12.8))
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action == Action.CURTAIL
    assert plan.curtail_export is True


def test_hold_when_stored_energy_is_dearer_than_the_grid():
    """The everyday hold: the battery is full and its energy is worth more
    than the current buy price (a big window beyond the horizon), so the
    plan serves the house from the grid — where 'idle' actuation would
    quietly discharge the battery into the load."""
    settings = make_settings(optimizer={"terminal_soc_value": 0.5})
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    data = replace(data, inputs=replace(data.inputs, soc0_kwh=12.8))
    plan = planner.optimize(data, NOW)
    assert plan.intervals[0].action == Action.HOLD
    assert plan.intervals[0].grid_import_kw == pytest.approx(0.5, abs=0.05)
    assert plan.curtail_export is False  # positive prices: no cap wanted


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
        grid,
        ADELAIDE,
        target_soc=1.0,
        target_time=dt_time(15, 0),
        hold_hours=0.0,
        capacity_kwh=44.8,
    )
    assert instant is not None and len(instant) == len(grid)  # aligned with soc[1:]
    assert instant[10] == pytest.approx(44.8)  # soc[11] == 05:30 UTC == 15:00 Adelaide
    assert list(np.nonzero(instant)[0]) == [10, 58]  # today + tomorrow, one step each

    # a 2-hour hold widens each day's floor to the step-ends in [15:00, 17:00]
    windowed = daily_soc_target_vector(
        grid,
        ADELAIDE,
        target_soc=1.0,
        target_time=dt_time(15, 0),
        hold_hours=2.0,
        capacity_kwh=44.8,
    )
    assert windowed is not None
    assert list(np.nonzero(windowed)[0]) == [10, 11, 12, 13, 14, 58, 59, 60, 61, 62]

    # clamp to soc_max so an over-100% target can't bake in a phantom penalty
    clamped = daily_soc_target_vector(
        grid,
        ADELAIDE,
        target_soc=1.0,
        target_time=dt_time(15, 0),
        hold_hours=0.0,
        capacity_kwh=44.8,
        soc_max_kwh=40.0,
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
        g2,
        ADELAIDE,
        target_soc=1.0,
        target_time=dt_time(15, 0),
        hold_hours=4.0,
        capacity_kwh=44.8,
    )
    assert elapsed is not None
    assert elapsed[0] == 0.0  # step 0 not pinned
    assert np.count_nonzero(elapsed) == 9  # only tomorrow's 4h window (9 step-ends)

    # disabled
    assert (
        daily_soc_target_vector(
            grid,
            ADELAIDE,
            target_soc=0.0,
            target_time=dt_time(15, 0),
            hold_hours=4.0,
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
        buffered = await make_planner(client, make_settings(load={"buffer": 0.25})).gather(NOW)
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
