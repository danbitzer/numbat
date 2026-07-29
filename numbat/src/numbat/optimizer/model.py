"""The MILP: battery charge/discharge scheduling over the rolling horizon.

Formulation:

    min  Σ (buy·gi − sell·ge)·Δt            energy cost/revenue
       + wear · Σ pd·Δt                      battery wear on discharge
       + spread · Σ pd_sell·Δt               margin bar on battery-sourced export
       + ε · Σ (pc + pd)·Δt                  anti-chatter tiebreak
       + reserve_penalty · Σ slack·Δt        soft spike-reserve violations
       + target_penalty · Σ tslack           soft daily-SoC-target shortfall
       − v_T · soc[T]                        terminal SoC value

    s.t. pv_u + pd + gi == load + pc + ge    power balance per step
         0 ≤ pv_u ≤ pv                       curtailment allowed
         soc[t+1] == soc[t] + (ηc·pc − pd/ηd)·Δt
         soc bounds; pc ≤ Pc·y; pd ≤ Pd·(1−y)   no simultaneous charge+discharge
         gi ≤ Gi; ge ≤ Ge
         soc[t] ≥ reserve[t] − slack[t]      soft floor (spike readiness)
         soc[k] ≥ target[k] − tslack[k]      soft instants (daily full-charge)
         optional: pc ≤ pv_u                 (allow_grid_charge=false)

Grid shape varies per cycle (data-driven), so the problem is rebuilt each
solve — HiGHS handles ~80 steps in tens of ms, so parameter caching isn't
worth the fixed-shape constraint it would impose.

Note: scalar boolean cvxpy Variables crash on solution unpacking with HiGHS
(cvxpy 1.6.x bug) — y is always a vector here, which is fine.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import cvxpy as cp
import numpy as np

log = logging.getLogger(__name__)

EPSILON_CHATTER = 0.0005  # $/kWh tiebreak against pointless cycling
SELL_BUY_MARGIN = 0.001  # enforced sell < buy gap, $/kWh


@dataclass(frozen=True)
class BatteryParams:
    capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    efficiency_charge: float
    efficiency_discharge: float
    soc_min_kwh: float
    soc_max_kwh: float
    wear_cost_per_kwh: float
    allow_grid_charge: bool
    # Battery→grid sales may never take (or leave) SoC below this; serving
    # the house's uncovered load still may, down to soc_min_kwh. 0 = off.
    export_reserve_kwh: float = 0.0


@dataclass(frozen=True)
class GridParams:
    import_limit_kw: float
    export_limit_kw: float
    # Below this feed-in price ($/kWh), forbid BATTERY-sourced grid export —
    # the battery still covers the house, but never sells stored energy this
    # cheap. PV surplus can still export. None = no manual floor (the dynamic
    # spread in OptimizerConfig still applies).
    min_battery_export_price: float | None = None


@dataclass(frozen=True)
class OptimizerInputs:
    dt_hours: np.ndarray  # step widths, hours
    buy: np.ndarray  # $/kWh per step
    sell: np.ndarray
    pv: np.ndarray  # kW per step
    load: np.ndarray
    soc0_kwh: float
    reserve_kwh: np.ndarray | None = None  # soft SoC floor per step (spike readiness)
    # Per-step discharge cap override (kW); None -> battery.max_discharge_kw
    # everywhere. Used to raise the cap during a confirmed spike interval.
    max_discharge_kw_step: np.ndarray | None = None
    # Soft daily SoC target FLOOR, length T aligned with soc[1:] (same
    # convention as reserve_kwh); 0 = inactive. The daily full-charge
    # insurance: held across a window (target time through the evening peak),
    # so "be full for the evening" actually keeps it full for the evening, not
    # just at a single 3pm instant. The battery is free to discharge once the
    # window ends.
    soc_target_kwh: np.ndarray | None = None


@dataclass(frozen=True)
class OptimizerConfig:
    terminal_value: float  # $/kWh valuing residual stored energy (the hold value)
    # Slack cost accumulates per step: effectively $/kWh PER HOUR spent below
    # the reserve floor, so persistent violations cost more than momentary ones.
    reserve_penalty_per_kwh: float
    solver_timeout_s: float
    # $/kWh PER HOUR of shortfall below the (windowed) daily SoC target floor —
    # the insurance premium for not being full through the target window.
    soc_target_penalty_per_kwh: float = 0.0
    # Minimum arbitrage spread ($/kWh): a penalty per battery-sourced exported
    # kWh in the objective, so a sale must beat the plan's own best
    # alternative use of that energy (tonight's load, a cheaper rebuy, or
    # solar that would otherwise export) by this margin. 0 = off. The DYNAMIC
    # counterpart to grid.min_battery_export_price's fixed dollar floor —
    # kills pennies-margin export churn without blocking good sales above
    # cheap replacement.
    min_battery_export_spread: float = 0.0
    # Self-sufficiency bias ($/kWh): a VIRTUAL toll added to every imported kWh
    # in the objective only — never in displayed costs (those are recomputed
    # from raw prices in result.py). A risk-preference knob, not economics:
    # import-dependent bets (charge now, sell into a forecast peak later) must
    # beat holding/solar by this much more, since the import is certain money
    # and the forecast sell is not. Gated OFF at negative buy prices — being
    # paid to import must stay attractive. 0 = off.
    import_penalty_per_kwh: float = 0.0


@dataclass
class Solution:
    status: str
    objective: float
    solve_ms: float
    charge_kw: np.ndarray
    discharge_kw: np.ndarray
    grid_import_kw: np.ndarray
    grid_export_kw: np.ndarray
    pv_used_kw: np.ndarray
    soc_kwh: np.ndarray  # length T+1

    @property
    def ok(self) -> bool:
        return self.status in ("optimal", "optimal_inaccurate")


class SolverError(Exception):
    pass


def auto_terminal_value(
    buy: np.ndarray,
    battery: BatteryParams,
    *,
    floor: float = 0.01,
    scaling: float = 1.0,
) -> float:
    """The hold value: what a kWh left in the battery at the horizon's end is
    worth. Anchored to REBUY COST — the cheapest forward import price grossed
    up for charge losses (min(buy) / efficiency_charge) — because that is what
    it would cost to put that energy back. Scaled, then floored ABOVE ZERO so a
    cheap day never values stored energy at ~$0.

    The rebuy anchor does NOT subtract wear, so on a volatile day wear enters
    the export threshold with the right sign (sell must clear wear +
    hold/eff_discharge, so more wear means LESS export — the old
    median*eta - wear formula had this backwards, which is why raising wear made
    the cheap selling worse).

    One guard: on a FLAT or low-spread horizon the rebuy anchor (~current price
    grossed up) would exceed the break-even for self-consuming stored energy, so
    the battery would hoard and import to run the house — technically a wash but
    a bad look, and it strands the free solar already in the pack. Cap the hold
    value at that break-even (median*eta_d - wear) so a flat day still self-
    consumes. Where the cap binds it does reintroduce the wear term, but only on
    low-spread horizons with no arbitrage to protect — and Amber feed-in sits
    well below buy, so it does not reopen cheap export in practice. See CHANGELOG
    for the full rationale.

    Scaling multiplies the rebuy anchor only (not the cap): a scaling > 1 makes
    the battery holdier without lifting the hold value past the self-consumption
    break-even, which would defeat the cap."""
    rebuy = float(np.min(buy)) / battery.efficiency_charge
    self_consumption_cap = (
        float(np.median(buy)) * battery.efficiency_discharge - battery.wear_cost_per_kwh
    )
    return max(floor, min(scaling * rebuy, self_consumption_cap))


def solve(
    inputs: OptimizerInputs,
    battery: BatteryParams,
    grid: GridParams,
    config: OptimizerConfig,
    pin_step0: str | None = None,
) -> Solution:
    """pin_step0 constrains the first step's battery mode ('charge' /
    'discharge' / 'idle') — used by the planner's hysteresis to price the
    previous action before allowing a switch.

    Actions are grid-coupled (see classify_action): 'idle' pins step 0 to the
    self-consumption envelope (charge from PV only, no battery export), NOT a
    frozen battery — the inverter's idle mode still serves load and soaks up
    PV surplus."""
    T = len(inputs.dt_hours)
    if not (len(inputs.buy) == len(inputs.sell) == len(inputs.pv) == len(inputs.load) == T):
        raise ValueError("all input arrays must have the same length")
    dt = inputs.dt_hours
    buy = inputs.buy
    # Keep sell strictly below buy so simultaneous import+export is never
    # optimal (true for Amber anyway; guards degenerate LP directions).
    sell = np.minimum(inputs.sell, buy - SELL_BUY_MARGIN)
    # Start from the ACTUAL SoC, even below soc_min — clamping it up to the
    # floor invents energy that isn't there (seen live: a BMS recalibration
    # dropped the real SoC below soc_min and the plan kept
    # spending the phantom 4+ kWh). The hard floor relaxes to the actual
    # start, so a below-reserve battery can never be discharged further and
    # recovers above soc_min when prices make charging worthwhile. Clip only
    # to the physical [0, soc_max] against sensor glitches.
    soc0 = float(np.clip(inputs.soc0_kwh, 0.0, battery.soc_max_kwh))
    soc_floor = min(battery.soc_min_kwh, soc0)

    pc = cp.Variable(T, nonneg=True)
    pd = cp.Variable(T, nonneg=True)
    gi = cp.Variable(T, nonneg=True)
    ge = cp.Variable(T, nonneg=True)
    pv_u = cp.Variable(T, nonneg=True)
    soc = cp.Variable(T + 1)
    y = cp.Variable(T, boolean=True)

    constraints = [
        pv_u + pd + gi == inputs.load + pc + ge,
        pv_u <= inputs.pv,
        soc[0] == soc0,
        soc[1:]
        == soc[:-1]
        + cp.multiply(battery.efficiency_charge * pc - pd / battery.efficiency_discharge, dt),
        soc >= soc_floor,
        soc <= battery.soc_max_kwh,
        pc <= battery.max_charge_kw * y,
        pd
        <= cp.multiply(
            inputs.max_discharge_kw_step
            if inputs.max_discharge_kw_step is not None
            else np.full(T, battery.max_discharge_kw),
            1 - y,
        ),
        gi <= grid.import_limit_kw,
        ge <= grid.export_limit_kw,
    ]
    if not battery.allow_grid_charge:
        constraints.append(pc <= pv_u)
    # Manual export price floor: below this feed-in price, cap the battery's
    # DISCHARGE at the house load NOT already covered by PV, so stored energy
    # covers the house but never routes to the grid — not even indirectly by
    # displacing PV that then exports. Grid import/charging and PV export are
    # untouched. A fixed dollar guarantee, deliberately static — that is its
    # documented job; the automatic spread is dynamic (see pd_sell below).
    # NB cap pd at the RESIDUAL load (load - pv), not the full load: capping at
    # full load lets the battery serve the whole house while PV exports below
    # the floor — the stored kWh reaching the grid by substitution. And do NOT
    # bound `ge <= pv_u - pc` instead: with ge >= 0 that forces pc <= pv_u,
    # forbidding grid charging overnight (pv_u = 0) at exactly the cheap,
    # low-feed-in windows you want to charge in. Uses the raw forecast sell.
    residual_load = np.maximum(0.0, inputs.load - inputs.pv)
    if grid.min_battery_export_price is not None:
        below = np.where(inputs.sell < grid.min_battery_export_price)[0]
        if below.size:
            constraints.append(pd[below] <= residual_load[below])
    # Battery-sourced export, pd_sell: the residual-load cap on the remainder
    # (pd - pd_sell <= residual) forces any discharge beyond the house's
    # uncovered load to carry the pd_sell label, so stored energy cannot reach
    # the grid unlabelled even by displacing PV. Shared by two features,
    # created once when either is active:
    #   * The DYNAMIC export spread — a penalty per sold kWh in the objective,
    #     so selling must beat the plan's own best alternative use of the
    #     energy (tonight's load, a cheaper rebuy, or solar that would
    #     otherwise export) by the configured margin. Deliberately NOT a
    #     static price gate off the hold value: on a solar-refill day the
    #     true replacement cost is the forgone feed-in, far below the hold
    #     value, and a static gate blocks sales the economics support (seen
    #     live 2026-07-29: a 28c sell refused on a 20c-hold, 40 kWh-PV day).
    #   * The export reserve — sales may never take (or leave) SoC below the
    #     reserve; the house may still draw down to the hard floor. One-way:
    #     never forces charging back above itself. Its condition is on SoC (a
    #     decision variable), so it needs a binary: sell_ok[t]=1 permits
    #     selling at step t and requires that step to END at or above the
    #     reserve (sell down TO it, never through it).
    # Only the reserve adds binaries; the spread alone stays pure LP, and
    # with both off no pd_sell machinery is built at all.
    spread_active = config.min_battery_export_spread > 0
    reserve_active = battery.export_reserve_kwh > soc_floor
    pd_sell = None
    if spread_active or reserve_active:
        pd_sell = cp.Variable(T, nonneg=True)
        constraints += [pd_sell <= pd, pd - pd_sell <= residual_load]
    if reserve_active:
        sell_ok = cp.Variable(T, boolean=True)
        pd_cap = (
            inputs.max_discharge_kw_step
            if inputs.max_discharge_kw_step is not None
            else np.full(T, battery.max_discharge_kw)
        )
        constraints += [
            pd_sell <= cp.multiply(pd_cap, sell_ok),
            soc[1:]
            >= battery.export_reserve_kwh
            - (battery.export_reserve_kwh - soc_floor) * (1 - sell_ok),
        ]
    # The self-consumption envelope: charge from PV only, export only PV
    # leftovers (no battery export); serving load from the battery is free.
    self_consumption = [pc[0] <= pv_u[0], ge[0] <= pv_u[0] - pc[0]]
    if pin_step0 == "charge":
        constraints += [pd[0] == 0, pc[0] >= 0.01]
    elif pin_step0 == "discharge":
        constraints += [pc[0] == 0, pd[0] >= 0.01]
    elif pin_step0 == "no_charge":
        constraints += [*self_consumption, pc[0] == 0]  # block charging
    elif pin_step0 in ("idle", "curtail"):
        constraints += self_consumption

    cost = (
        cp.sum(cp.multiply(buy, cp.multiply(gi, dt)))
        - cp.sum(cp.multiply(sell, cp.multiply(ge, dt)))
        + battery.wear_cost_per_kwh * cp.sum(cp.multiply(pd, dt))
        + EPSILON_CHATTER * cp.sum(cp.multiply(pc + pd, dt))
        - config.terminal_value * soc[T]
    )
    if spread_active and pd_sell is not None:
        # The dynamic spread: a margin bar per sold kWh (battery-side, like
        # wear). Because it sits in the objective, "profit" is measured
        # against the plan's OWN valuation of the energy, whatever the
        # marginal alternative is this horizon.
        cost = cost + config.min_battery_export_spread * cp.sum(cp.multiply(pd_sell, dt))
    if config.import_penalty_per_kwh > 0:
        # Import reluctance: virtual toll per imported kWh (see OptimizerConfig).
        # Gated per step on the RAW buy price so negative-price windows keep
        # their full paid-to-charge appeal.
        toll = np.where(inputs.buy >= 0, config.import_penalty_per_kwh, 0.0)
        cost = cost + cp.sum(cp.multiply(toll, cp.multiply(gi, dt)))
    if (
        inputs.soc_target_kwh is not None
        and np.any(inputs.soc_target_kwh > 0)
        and config.soc_target_penalty_per_kwh > 0
    ):
        # Windowed soft floor (dt-weighted, like the reserve): the premium
        # accrues per kWh-hour of shortfall, so being short for the whole
        # evening costs more than a momentary dip. Aligned with soc[1:].
        target_slack = cp.Variable(T, nonneg=True)
        constraints.append(soc[1:] >= inputs.soc_target_kwh - target_slack)
        cost = cost + config.soc_target_penalty_per_kwh * cp.sum(
            cp.multiply(target_slack, dt)
        )
    if inputs.reserve_kwh is not None and np.any(inputs.reserve_kwh > 0):
        slack = cp.Variable(T, nonneg=True)
        constraints.append(soc[1:] >= inputs.reserve_kwh - slack)
        cost = cost + config.reserve_penalty_per_kwh * cp.sum(cp.multiply(slack, dt))

    problem = cp.Problem(cp.Minimize(cost), constraints)
    start = time.perf_counter()
    try:
        problem.solve(solver="HIGHS", time_limit=config.solver_timeout_s)
    except cp.error.SolverError as e:
        raise SolverError(str(e)) from e
    solve_ms = (time.perf_counter() - start) * 1000

    if problem.status not in ("optimal", "optimal_inaccurate") or soc.value is None:
        raise SolverError(f"solver returned status={problem.status}")

    return Solution(
        status=problem.status,
        objective=float(problem.value),
        solve_ms=solve_ms,
        charge_kw=np.asarray(pc.value).clip(min=0),
        discharge_kw=np.asarray(pd.value).clip(min=0),
        grid_import_kw=np.asarray(gi.value).clip(min=0),
        grid_export_kw=np.asarray(ge.value).clip(min=0),
        pv_used_kw=np.asarray(pv_u.value).clip(min=0),
        soc_kwh=np.asarray(soc.value),
    )
