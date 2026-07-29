"""Qualitative MILP scenarios: the behaviors the money depends on."""

import numpy as np
import pytest

from numbat.optimizer.model import (
    BatteryParams,
    GridParams,
    OptimizerConfig,
    OptimizerInputs,
    auto_terminal_value,
    solve,
)

BATTERY = BatteryParams(
    capacity_kwh=12.8,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    efficiency_charge=0.95,
    efficiency_discharge=0.95,
    soc_min_kwh=1.28,
    soc_max_kwh=12.8,
    wear_cost_per_kwh=0.04,
    allow_grid_charge=True,
)
GRID = GridParams(import_limit_kw=15.0, export_limit_kw=5.0)


def make_inputs(
    T: int = 24,
    dt: float = 0.5,
    buy: float | np.ndarray = 0.30,
    sell: float | np.ndarray = 0.10,
    pv: float | np.ndarray = 0.0,
    load: float | np.ndarray = 0.5,
    soc0: float = 6.4,
    reserve: np.ndarray | None = None,
    soc_target: np.ndarray | None = None,
) -> OptimizerInputs:
    def full(v) -> np.ndarray:
        return np.full(T, float(v)) if np.isscalar(v) else np.asarray(v, dtype=float)

    return OptimizerInputs(
        dt_hours=np.full(T, dt),
        buy=full(buy),
        sell=full(sell),
        pv=full(pv),
        load=full(load),
        soc0_kwh=soc0,
        reserve_kwh=reserve,
        soc_target_kwh=soc_target,
    )


def config(
    terminal_value: float, reserve_penalty: float = 0.5, target_penalty: float = 0.0
) -> OptimizerConfig:
    return OptimizerConfig(
        terminal_value=terminal_value,
        reserve_penalty_per_kwh=reserve_penalty,
        solver_timeout_s=30,
        soc_target_penalty_per_kwh=target_penalty,
    )


def test_scenario_price_spike_precharge_then_full_export():
    """Evening sell spike -> charge beforehand, dump at the export limit during.

    Realistic Amber spike: BOTH prices are high in the spike interval (buy =
    spot + network > sell = spot); the arbitrage is cross-interval — charge
    cheap earlier, export during the spike.
    """
    sell = np.full(24, 0.10)
    buy = np.full(24, 0.30)
    sell[10:12] = 5.0  # 1h spike
    buy[10:12] = 5.3
    inputs = make_inputs(buy=buy, sell=sell, soc0=2.0)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.25))
    # Grid-charges before the spike (buy 0.30 << sell 5.00 covers losses)
    assert sol.charge_kw[:10].sum() > 5.0
    # Discharges at max during the spike; export = discharge - house load
    # (importing at 5.30 to pad the 5.00 export would lose money)
    assert sol.discharge_kw[10] == pytest.approx(BATTERY.max_discharge_kw, abs=0.01)
    assert sol.discharge_kw[11] == pytest.approx(BATTERY.max_discharge_kw, abs=0.01)
    expected_export = BATTERY.max_discharge_kw - 0.5
    assert sol.grid_export_kw[10] == pytest.approx(expected_export, abs=0.01)
    # Never imports during the spike intervals
    assert sol.grid_import_kw[10:12].max() < 0.01


def test_scenario_negative_buy_price_grid_charges():
    """Negative overnight prices -> get paid to charge the battery."""
    buy = np.full(24, 0.30)
    buy[0:6] = -0.05
    inputs = make_inputs(buy=buy, sell=0.02, soc0=2.0)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.25))
    assert sol.charge_kw[0:6].sum() > 4.0
    assert sol.grid_import_kw[0:6].max() > 1.0


def test_scenario_negative_feed_in_curtails_solar():
    """Negative sell price + full battery -> spill excess PV rather than pay to export."""
    inputs = make_inputs(buy=0.25, sell=-0.10, pv=5.0, load=0.5, soc0=12.8)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.20))
    assert sol.grid_export_kw.max() < 0.01  # never pays to export
    assert sol.pv_used_kw.max() < 1.0  # curtailed down to ~load
    assert sol.grid_import_kw.max() < 0.01  # load still served by PV


def test_export_reserve_sells_down_to_it_never_through_it():
    """Fat feed-in all day: without a reserve the battery sells to the hard
    floor; with one, sales stop exactly at the reserve (the top half still
    sells at full value)."""
    inputs = make_inputs(sell=0.60, buy=0.65, load=0.0, soc0=9.6)
    base = solve(inputs, BATTERY, GRID, config(terminal_value=0.10))
    assert base.soc_kwh[-1] == pytest.approx(BATTERY.soc_min_kwh, abs=0.05)

    reserved = BatteryParams(**{**BATTERY.__dict__, "export_reserve_kwh": 6.4})
    sol = solve(inputs, reserved, GRID, config(terminal_value=0.10))
    assert sol.soc_kwh.min() >= 6.4 - 0.05  # never below the reserve
    assert sol.grid_export_kw.sum() * 0.5 > 2.5  # the top ~3 kWh still sold


def test_export_reserve_house_serving_continues_below_it():
    """Starting BELOW the reserve with dear imports: the battery still runs
    the house down toward the hard floor — no sales, no forced imports to
    defend the reserve, and no forced recharge back above it."""
    inputs = make_inputs(sell=0.28, buy=0.30, load=1.0, soc0=3.0)
    reserved = BatteryParams(**{**BATTERY.__dict__, "export_reserve_kwh": 6.4})
    sol = solve(inputs, reserved, GRID, config(terminal_value=0.05))
    assert sol.grid_export_kw.max() < 0.01  # can't sell below the reserve
    assert sol.discharge_kw.sum() * 0.5 > 1.0  # house served from the battery
    assert sol.soc_kwh[-1] < 6.4  # allowed to stay below; nothing forces a refill


def test_scenario_flat_prices_no_arbitrage_churn():
    """Flat prices -> no grid charging, no export; at most self-consumption."""
    inputs = make_inputs(buy=0.30, sell=0.10, load=1.0, soc0=6.4)
    terminal = auto_terminal_value(inputs.buy, BATTERY)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=terminal))
    assert sol.charge_kw.max() < 0.01
    assert sol.grid_export_kw.max() < 0.01


def test_scenario_terminal_value_prevents_horizon_drain():
    """Without terminal value the battery dumps at any positive sell price by
    horizon end; with it, residual energy is held."""
    inputs = make_inputs(T=12, buy=0.30, sell=0.10, load=0.5, soc0=10.0)
    drained = solve(inputs, BATTERY, GRID, config(terminal_value=0.0))
    held = solve(
        inputs, BATTERY, GRID, config(terminal_value=auto_terminal_value(inputs.buy, BATTERY))
    )
    assert drained.soc_kwh[-1] == pytest.approx(BATTERY.soc_min_kwh, abs=0.05)
    # Self-consumption discharge (load 0.5 kW x 6 h) is fine; exporting the
    # residual at sell=0.10 is not.
    assert held.soc_kwh[-1] > 6.5
    assert held.grid_export_kw.max() < 0.01


def test_scenario_spike_reserve_holds_energy():
    """Soft reserve floor keeps SoC available for a potential spike, unless
    the penalty is outweighed (it isn't here)."""
    # Attractive early sell price + low terminal value would normally drain it
    inputs_free = make_inputs(buy=0.60, sell=0.50, soc0=10.0)
    free = solve(inputs_free, BATTERY, GRID, config(terminal_value=0.05))
    assert free.soc_kwh[-1] == pytest.approx(BATTERY.soc_min_kwh, abs=0.05)

    reserve = np.full(24, 6.0)
    inputs_held = make_inputs(buy=0.60, sell=0.50, soc0=10.0, reserve=reserve)
    held = solve(inputs_held, BATTERY, GRID, config(terminal_value=0.05, reserve_penalty=5.0))
    assert held.soc_kwh[1:].min() >= 6.0 - 0.05


def test_scenario_spike_reserve_yields_to_bigger_opportunity():
    """The reserve is soft: a confirmed spike RIGHT NOW (no time to pre-charge)
    is worth more than the slack penalty, so the floor is broken to sell.

    (With any lead time the optimizer prefers to grid-charge first and keep
    the reserve intact — verified by the passing pre-charge scenario above.)
    """
    sell = np.full(24, 0.10)
    buy = np.full(24, 0.30)
    sell[0] = 8.0  # spike in the current interval
    buy[0] = 8.3
    reserve = np.full(24, 6.0)
    inputs = make_inputs(buy=buy, sell=sell, soc0=8.0, reserve=reserve)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.05, reserve_penalty=0.5))
    assert sol.discharge_kw[0] == pytest.approx(BATTERY.max_discharge_kw, abs=0.01)
    assert sol.soc_kwh[1] < 6.0  # broke the reserve to sell into the real spike


def test_no_grid_charge_option():
    battery = BatteryParams(**{**BATTERY.__dict__, "allow_grid_charge": False})
    buy = np.full(24, 0.30)
    buy[0:6] = 0.01  # tempting grid charge
    inputs = make_inputs(buy=buy, sell=0.10, pv=0.0, soc0=2.0)
    sol = solve(inputs, battery, GRID, config(terminal_value=0.25))
    assert sol.charge_kw.max() < 0.01  # no PV -> no charging allowed


def test_soc_sensor_glitch_clamped_not_infeasible():
    inputs = make_inputs(soc0=15.0)  # above capacity
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.2))
    assert sol.ok
    assert sol.soc_kwh[0] == pytest.approx(BATTERY.soc_max_kwh)


def spike_prices() -> tuple[np.ndarray, np.ndarray]:
    buy = np.full(24, 0.30)
    sell = np.full(24, 0.10)
    buy[10:12], sell[10:12] = 5.3, 5.0
    return buy, sell


def test_no_simultaneous_charge_discharge():
    buy, sell = spike_prices()
    inputs = make_inputs(buy=buy, sell=sell, soc0=2.0)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.25))
    assert (np.minimum(sol.charge_kw, sol.discharge_kw) < 0.01).all()


def test_energy_balance_holds():
    buy, sell = spike_prices()
    inputs = make_inputs(buy=buy, sell=sell, pv=2.0, load=1.0, soc0=5.0)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.25))
    lhs = sol.pv_used_kw + sol.discharge_kw + sol.grid_import_kw
    rhs = inputs.load + sol.charge_kw + sol.grid_export_kw
    assert np.allclose(lhs, rhs, atol=1e-4)


def test_classify_action_grid_coupled_semantics():
    from numbat.models import Action
    from numbat.optimizer.result import classify_action

    # (charge, discharge, pv, pv_used, load) -> expected
    cases = [
        # battery runs the house at night: self-consumption, not DISCHARGE
        ((0.0, 0.9, 0.0, 0.0, 0.9), Action.IDLE),
        # battery exports beyond the load deficit: forced discharge territory
        ((0.0, 5.0, 0.0, 0.0, 0.9), Action.DISCHARGE),
        # daytime: discharge tops up what PV can't cover -> still idle
        ((0.0, 0.5, 1.0, 1.0, 1.5), Action.IDLE),
        # charging from PV surplus: self-consumption does this natively
        ((3.0, 0.0, 5.0, 5.0, 2.0), Action.IDLE),
        # charging beyond the PV surplus: grid charge
        ((5.0, 0.0, 2.0, 2.0, 1.0), Action.CHARGE),
        # spilling PV on purpose (negative feed-in)
        ((0.0, 0.0, 5.0, 1.0, 1.0), Action.CURTAIL),
        # battery flat while PV surplus is exported, not stored: defer charge
        ((0.0, 0.0, 5.0, 5.0, 2.0), Action.NO_CHARGE),
        # battery flat while load imports (reserve held): idle for now
        ((0.0, 0.0, 0.0, 0.0, 0.5), Action.IDLE),
        # nothing flowing at all
        ((0.0, 0.0, 0.0, 0.0, 0.0), Action.IDLE),
    ]
    for args, expected in cases:
        assert classify_action(*args) == expected, args


def test_scenario_defer_charge_to_cheaper_window():
    """Dan's live morning (2026-07-17): good feed-in now, worthless feed-in
    at midday -> export the morning PV, charge the battery later. Step 0
    must classify NO_CHARGE (self-consumption mode would charge immediately)."""
    from numbat.models import Action
    from numbat.optimizer.result import classify_action

    T = 12
    # 0.28 sell: beats storing PV (terminal ~0.24) but not exporting stored
    # energy (wear + terminal/eff ~0.30) — so the battery must simply wait.
    # Late PV is modest so the battery does NOT saturate (a saturating
    # battery makes early discharge optimal: stored energy loses its
    # terminal value once the horizon ends full).
    sell = np.concatenate([np.full(6, 0.28), np.full(6, 0.0)])
    pv = np.concatenate([np.full(6, 5.0), np.full(6, 2.0)])
    inputs = make_inputs(T=T, buy=0.35, sell=sell, pv=pv, load=0.5, soc0=2.0)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.25))
    step0 = classify_action(
        float(sol.charge_kw[0]),
        float(sol.discharge_kw[0]),
        float(inputs.pv[0]),
        float(sol.pv_used_kw[0]),
        float(inputs.load[0]),
    )
    assert step0 == Action.NO_CHARGE  # exporting at 0.28, not storing
    # the charge happens in the worthless-feed-in window instead
    assert float(np.max(sol.charge_kw[6:])) > 1.0
    assert float(np.max(sol.charge_kw[:6])) < 0.05


def test_pin_no_charge_blocks_charging_allows_discharge():
    # expensive import + cheap stored energy: serving load from the battery is
    # optimal. Pinning no_charge must forbid charging yet still permit that
    # discharge (unlike a battery-frozen pin).
    inputs = make_inputs(T=6, buy=0.40, sell=0.05, pv=0.0, load=2.0, soc0=6.4)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.05), pin_step0="no_charge")
    assert float(sol.charge_kw[0]) == pytest.approx(0.0, abs=1e-6)  # no charging
    assert float(sol.discharge_kw[0]) > 0.0  # serving load is still allowed


def test_soc_below_floor_starts_honest_and_recovers():
    """A real SoC below soc_min (BMS recalibration, overnight self-consumption
    drain) must not be clamped up to the floor: that invents phantom energy the
    plan then spends. The plan starts from the actual SoC, never discharges
    below it, and charges back above the floor when prices favor it.
    """
    buy = np.full(24, 0.30)
    buy[4:8] = 0.05  # a cheap window to recover in
    inputs = make_inputs(buy=buy, sell=0.02, load=0.5, soc0=0.6)  # floor is 1.28
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.25))
    assert sol.soc_kwh[0] == pytest.approx(0.6)  # honest start, not 1.28
    assert float(np.min(sol.soc_kwh)) >= 0.6 - 1e-6  # never digs deeper
    assert float(np.min(sol.discharge_kw[:4])) == pytest.approx(0.0, abs=1e-6)
    assert float(sol.soc_kwh[-1]) >= BATTERY.soc_min_kwh  # recovered above floor


def test_soc_glitch_above_capacity_still_clamped():
    inputs = make_inputs(soc0=99.0)  # sensor glitch beyond physical capacity
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.05))
    assert sol.soc_kwh[0] == pytest.approx(BATTERY.soc_max_kwh)


def test_empty_battery_flat_prices_serves_load_by_import_not_charging():
    """An empty battery must NOT be preemptively charged to serve later load:
    direct import (AC passthrough in self-consumption) skips the AC-DC round
    trip, so at flat prices charge-then-discharge is strictly worse than
    import. Grid charging from empty must only appear for real arbitrage.
    """
    inputs = make_inputs(buy=0.30, sell=0.10, load=1.5, soc0=0.0)
    terminal = auto_terminal_value(inputs.buy, BATTERY)
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=terminal))
    assert sol.charge_kw.max() < 0.01  # never charges just to re-serve load
    assert float(np.min(sol.grid_import_kw)) >= 1.5 - 1e-6  # load fed directly


def _target_window(T: int, lo: int, hi: int, kwh: float) -> np.ndarray:
    """Windowed daily-target floor (length T, aligned with soc[1:]): require
    kwh across the end-of-step SoCs soc[lo+1 .. hi]."""
    target = np.zeros(T)
    target[lo:hi] = kwh
    return target


def test_daily_target_fills_in_cheap_window():
    """The soft daily target pulls a full charge through a cheap window that
    pure economics would ignore (sell 0, terminal 0: stored energy is
    worthless in-model — exactly the unforecast-spike insurance case)."""
    buy = np.full(12, 0.30)
    buy[1:8] = 0.05  # cheap window (long enough to fill at max charge power)
    free = make_inputs(T=12, buy=buy, sell=0.0, load=0.0, soc0=2.0)
    sol_free = solve(free, BATTERY, GRID, config(terminal_value=0.0))
    assert sol_free.charge_kw.max() < 0.01  # control: no reason to charge

    # floor across soc[8], soc[9] (window steps 7..8)
    inputs = make_inputs(
        T=12, buy=buy, sell=0.0, load=0.0, soc0=2.0,
        soc_target=_target_window(12, 7, 9, BATTERY.soc_max_kwh),
    )
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.0, target_penalty=0.30))
    assert sol.soc_kwh[8] == pytest.approx(BATTERY.soc_max_kwh, abs=1e-3)
    # the fill happened inside the cheap window, not at 0.30
    assert float(sol.charge_kw[0]) < 0.01


def test_daily_target_yields_when_filling_costs_more_than_the_premium():
    """Soft, not dumb: at buy 0.40 the marginal fill costs ~0.42/kWh; a small
    premium (0.10/kWh-hour over a 1h window) doesn't justify overpaying."""
    inputs = make_inputs(
        T=12, buy=0.40, sell=0.0, load=0.0, soc0=2.0,
        soc_target=_target_window(12, 7, 9, BATTERY.soc_max_kwh),
    )
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.0, target_penalty=0.10))
    assert sol.charge_kw.max() < 0.01  # refused to fill at a loss
    assert sol.soc_kwh[8] == pytest.approx(2.0, abs=1e-3)


def test_daily_target_holds_through_window_then_frees():
    """A FLOOR across the window (the redesign): full through the whole target
    window, then free to discharge once it ends — not a single instant it can
    dump the moment after."""
    buy = np.full(12, 0.30)
    buy[1:6] = 0.05
    sell = np.zeros(12)
    sell[9:] = 0.50  # good export window AFTER the target window ends
    # floor across soc[7], soc[8], soc[9] (window steps 6..8)
    inputs = make_inputs(
        T=12, buy=buy, sell=sell, load=0.0, soc0=2.0,
        soc_target=_target_window(12, 6, 9, BATTERY.soc_max_kwh),
    )
    sol = solve(inputs, BATTERY, GRID, config(terminal_value=0.0, target_penalty=0.30))
    # held full across the WHOLE window, not just one instant
    assert sol.soc_kwh[7] == pytest.approx(BATTERY.soc_max_kwh, abs=1e-2)
    assert sol.soc_kwh[9] == pytest.approx(BATTERY.soc_max_kwh, abs=1e-2)
    # ...then discharges flat-out into the sell window: floor lifts after
    assert np.allclose(sol.discharge_kw[9:], BATTERY.max_discharge_kw, atol=0.01)
    assert sol.soc_kwh[-1] < BATTERY.soc_max_kwh - 7.0  # well below the target


# ---- the hold-value redesign -------------------------------------------------


def test_hold_value_anchors_to_rebuy_and_floors_above_zero():
    """On a day with real spread the hold value is the cheapest forward import
    grossed up for charge loss (the rebuy anchor), floored above zero — never
    ~$0 (that was the cheap-export bug), and independent of wear so raising wear
    can't invert the export decision."""
    buy = np.concatenate([np.full(6, 0.05), np.full(18, 0.30)])  # wide spread
    hv = auto_terminal_value(buy, BATTERY)
    assert hv == pytest.approx(0.05 / BATTERY.efficiency_charge, abs=1e-6)  # rebuy binds
    # a high-wear battery gets the SAME hold value (rebuy has no wear term)
    worn = BatteryParams(**{**BATTERY.__dict__, "wear_cost_per_kwh": 0.20})
    assert auto_terminal_value(buy, worn) == pytest.approx(hv, abs=1e-6)
    # the floor bites when every forward price is negative
    neg = np.full(12, -0.05)
    assert auto_terminal_value(neg, BATTERY, floor=0.01) == pytest.approx(0.01)
    # a flat horizon caps at the self-consumption break-even (median*eta - wear),
    # BELOW rebuy, so the battery still runs the house instead of hoarding
    flat = np.full(12, 0.30)
    cap = 0.30 * BATTERY.efficiency_discharge - BATTERY.wear_cost_per_kwh
    assert auto_terminal_value(flat, BATTERY) == pytest.approx(cap, abs=1e-6)
    # scaling multiplies the rebuy anchor only, never lifting past the cap (or
    # the self-consumption guard would be defeated)
    assert auto_terminal_value(flat, BATTERY, scaling=1.5) == pytest.approx(cap, abs=1e-6)


def test_higher_wear_reduces_export_not_increases_it():
    """The sign fix: with the rebuy anchor, raising wear lifts the export
    threshold, so a wear-conscious battery sells LESS — the opposite of the old
    median*eta - wear formula, which made more wear invite cheap selling."""
    buy = np.concatenate([np.full(10, 0.10), np.full(14, 0.20)])
    sell = np.full(24, 0.08)
    sell[12:14] = 0.30  # a modest sell bump
    kept = 0
    for wear in (0.02, 0.25):
        b = BatteryParams(**{**BATTERY.__dict__, "wear_cost_per_kwh": wear})
        hv = auto_terminal_value(buy, b)
        sol = solve(make_inputs(buy=buy, sell=sell, soc0=10.0), b, GRID,
                    config(terminal_value=hv))
        if sol.grid_export_kw[12] < 0.05:
            kept += 1
    assert kept == 1  # low wear exports into the bump, high wear holds


def test_export_spread_is_dynamic_solar_refill_day_still_sells():
    """The 2026-07-29 live case: a 28c feed-in blip on a day when solar will
    refill the battery within hours. The hold value is high (no cheap grid
    window), so the OLD static gate (hold/eff + wear + spread ~ 32c) refused
    the sale — but the true replacement cost is the ~10c feed-in the
    refilling solar would otherwise have earned, so the sale nets ~+12c/kWh
    and must clear a 5c spread."""
    sell = np.full(24, 0.10)
    sell[:2] = 0.28  # the blip, before the solar arrives
    pv = np.zeros(24)
    pv[6:16] = 6.0  # 30 kWh of midday solar: refills the battery, then exports
    inputs = make_inputs(buy=0.32, sell=sell, pv=pv, load=0.5, soc0=10.0)
    sol = solve(
        inputs, BATTERY, GRID,
        OptimizerConfig(terminal_value=0.22, reserve_penalty_per_kwh=0.5,
                        solver_timeout_s=30, min_battery_export_spread=0.05),
    )
    # Sells hard into the blip (load 0.5, pv 0 -> export is battery-sourced)...
    assert sol.grid_export_kw[:2].max() > 3.0
    # ...and ends refilled by the solar, not drained
    assert sol.soc_kwh[-1] > 9.0


def test_min_battery_export_spread_suppresses_thin_export():
    """The margin bar: a feed-in that only just beats holding is not
    worth the cycle. A 0.02 spread outweighs the thin margin and the plan
    holds instead of selling."""
    buy = np.full(24, 0.30)
    buy[:6] = 0.08  # cheap overnight -> low rebuy hold value
    inputs = make_inputs(buy=buy, sell=0.14, pv=0.0, load=0.5, soc0=10.0)
    hv = auto_terminal_value(buy, BATTERY)
    thin = solve(inputs, BATTERY, GRID, config(terminal_value=hv))
    guarded = solve(inputs, BATTERY, GRID,
                    OptimizerConfig(terminal_value=hv, reserve_penalty_per_kwh=0.5,
                                    solver_timeout_s=30, min_battery_export_spread=0.02))
    assert thin.grid_export_kw.max() > 1.0  # would export without the margin bar
    assert guarded.grid_export_kw.max() < 0.01  # the margin bar holds it


def test_export_spread_never_taxes_serving_the_house():
    """The penalty must sit on pd_sell only: an absurd 5 $/kWh spread may not
    stop the battery running the house at night. (Mutation-verified: a
    penalty applied to total pd survives the rest of the suite but freezes
    self-consumption here.)"""
    inputs = make_inputs(buy=0.40, sell=0.05, pv=0.0, load=2.0, soc0=6.4)
    sol = solve(inputs, BATTERY, GRID,
                OptimizerConfig(terminal_value=0.05, reserve_penalty_per_kwh=0.5,
                                solver_timeout_s=30, min_battery_export_spread=5.0))
    assert sol.discharge_kw.sum() * 0.5 > 1.0  # house still served from battery
    assert sol.grid_export_kw.max() < 0.01


def test_export_spread_is_per_kwh_not_per_step():
    """dt-weighting probe: with sell 15.5c the true bar (hold 8.4c grossed +
    wear + 2c spread) is ~14.9c -> sells; a penalty missing the dt multiply
    doubles the effective spread at 0.5 h steps (~16.9c bar) and flips the
    decision. (Mutation-verified discriminator.)"""
    buy = np.full(24, 0.30)
    buy[:6] = 0.08
    inputs = make_inputs(buy=buy, sell=0.155, pv=0.0, load=0.5, soc0=10.0)
    hv = auto_terminal_value(buy, BATTERY)
    sol = solve(inputs, BATTERY, GRID,
                OptimizerConfig(terminal_value=hv, reserve_penalty_per_kwh=0.5,
                                solver_timeout_s=30, min_battery_export_spread=0.02))
    assert sol.grid_export_kw.max() > 1.0  # the ~+0.6c margin clears a true 2c bar


def test_min_battery_export_price_blocks_indirect_export_via_pv_displacement():
    """The manual floor caps battery discharge at the load PV can't cover, so
    stored energy never reaches the grid even indirectly. With PV > load the
    battery is fully blocked (PV covers the house and exports the surplus);
    without the floor it would discharge to free even more PV for export."""
    inputs = make_inputs(buy=0.30, sell=0.10, pv=4.0, load=0.5, soc0=10.0)
    no_floor = GridParams(import_limit_kw=15.0, export_limit_kw=5.0)
    floored = GridParams(import_limit_kw=15.0, export_limit_kw=5.0, min_battery_export_price=0.15)
    free = solve(inputs, BATTERY, no_floor, config(terminal_value=0.05))
    held = solve(inputs, BATTERY, floored, config(terminal_value=0.05))
    assert free.discharge_kw.max() > 1.0  # unconstrained: battery sells via PV
    assert held.discharge_kw.max() < 0.01  # PV already covers load -> fully blocked
    assert held.grid_export_kw.max() == pytest.approx(3.5, abs=0.05)  # only PV surplus
    # and partial PV: battery may cover only the load PV can't (0.5 - 0.3 = 0.2)
    partial = make_inputs(buy=0.30, sell=0.10, pv=0.3, load=0.5, soc0=10.0)
    held2 = solve(partial, BATTERY, floored, config(terminal_value=0.05))
    assert held2.discharge_kw.max() < 0.21
    assert held2.grid_export_kw.max() < 0.01  # nothing left to export


def test_min_battery_export_price_does_not_block_grid_charging():
    """Regression: the floor must cap battery DISCHARGE, not force pc <= pv_u.
    Overnight (pv=0) grid charging into a cheap window must still happen."""
    buy = np.full(24, 0.30)
    buy[0:6] = 0.02  # cheap overnight window, below the export floor
    grid = GridParams(import_limit_kw=15.0, export_limit_kw=5.0, min_battery_export_price=0.15)
    inputs = make_inputs(buy=buy, sell=0.10, pv=0.0, load=0.5, soc0=2.0)
    sol = solve(inputs, BATTERY, grid, config(terminal_value=0.25))
    assert sol.charge_kw[0:6].sum() > 4.0  # grid-charged despite the floor
    assert sol.grid_import_kw[0:6].max() > 1.0


# ---- import reluctance (the self-sufficiency toll) ---------------------------


def test_import_penalty_suppresses_marginal_import_arbitrage():
    """A charge-at-20c-to-sell-at-30c bet clears a few cents without the toll
    and dies with it. Real opportunities (big spreads) survive — only thin,
    forecast-dependent bets are filtered. load=0 isolates the export bet (with
    house load present the toll correctly makes pre-charging for the house
    MORE attractive — later load imports pay the toll too)."""
    buy = np.full(24, 0.32)
    buy[0:6] = 0.20  # cheap-ish charge window
    sell = np.full(24, 0.05)
    sell[12:16] = 0.30  # modest sell window later (~4c/kWh net without toll)
    inputs = make_inputs(buy=buy, sell=sell, load=0.0, soc0=2.0)
    free = solve(inputs, BATTERY, GRID, config(terminal_value=0.05))
    tolled = solve(
        inputs, BATTERY, GRID,
        OptimizerConfig(terminal_value=0.05, reserve_penalty_per_kwh=0.5,
                        solver_timeout_s=30, import_penalty_per_kwh=0.08),
    )
    assert free.charge_kw[0:6].sum() > 3.0  # takes the thin bet
    assert tolled.charge_kw[0:6].sum() < 0.01  # toll kills it


def test_import_penalty_skips_negative_buy_prices():
    """The carve-out: being PAID to import must stay attractive — the toll
    never applies at negative buy prices."""
    buy = np.full(24, 0.30)
    buy[0:6] = -0.05
    inputs = make_inputs(buy=buy, sell=0.02, soc0=2.0)
    sol = solve(
        inputs, BATTERY, GRID,
        OptimizerConfig(terminal_value=0.25, reserve_penalty_per_kwh=0.5,
                        solver_timeout_s=30, import_penalty_per_kwh=0.10),
    )
    assert sol.charge_kw[0:6].sum() > 4.0  # still gets paid to charge
    assert sol.grid_import_kw[0:6].max() > 1.0


def test_import_penalty_never_blocks_unavoidable_load_imports():
    """No alternative exists (flat prices, empty battery): the toll shifts
    nothing — the house is still fed directly from the grid."""
    inputs = make_inputs(buy=0.30, sell=0.10, load=1.5, soc0=0.0)
    terminal = auto_terminal_value(inputs.buy, BATTERY)
    sol = solve(
        inputs, BATTERY, GRID,
        OptimizerConfig(terminal_value=terminal, reserve_penalty_per_kwh=0.5,
                        solver_timeout_s=30, import_penalty_per_kwh=0.10),
    )
    assert float(np.min(sol.grid_import_kw)) >= 1.5 - 1e-6  # load still fed
    assert sol.charge_kw.max() < 0.01  # and no phantom behavior appears
