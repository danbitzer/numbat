"""Map a raw MILP solution onto the Plan domain model."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from numbat.models import Action, Plan, PlanInterval
from numbat.optimizer.model import OptimizerInputs, Solution
from numbat.timegrid import TimeGrid

POWER_TOL_KW = 0.01
CURTAIL_TOL_KW = 0.05


def classify_action(
    charge_kw: float,
    discharge_kw: float,
    pv_kw: float,
    pv_used_kw: float,
    load_kw: float,
) -> Action:
    """Grid-coupled semantics: charge/discharge are reserved for battery moves
    a self-consumption inverter mode would NOT make on its own.

    - DISCHARGE: battery power beyond the house's unmet load — i.e. exporting
      stored energy (forced discharge is the right actuation: a pinned high
      setpoint, not load-following).
    - CHARGE: charging beyond the PV surplus — i.e. buying from the grid.
    - NO_CHARGE: battery idle while PV surplus is EXPORTED rather than stored —
      i.e. self-consumption would charge, but the plan defers the charge to a
      cheaper window. Block charging, still cover load dips.
    - IDLE: everything else self-consumption-shaped (running the house off the
      battery, charging from excess PV, or importing to serve load) — the
      inverter's native mode does this with second-by-second load tracking a
      5-min setpoint can't match. (The battery-idle-under-import case — hold
      the reserve while the grid serves load — also lands here for now; a
      future NO_DISCHARGE action would actuate it distinctly.)
    """
    export_discharge = discharge_kw - max(load_kw - pv_used_kw, 0.0)
    grid_charge = charge_kw - max(pv_used_kw - load_kw, 0.0)
    if export_discharge > POWER_TOL_KW:
        return Action.DISCHARGE
    if grid_charge > POWER_TOL_KW:
        return Action.CHARGE
    if pv_kw > CURTAIL_TOL_KW and pv_used_kw < pv_kw - CURTAIL_TOL_KW:
        return Action.CURTAIL
    battery_inactive = charge_kw <= POWER_TOL_KW and discharge_kw <= POWER_TOL_KW
    if battery_inactive and pv_used_kw - load_kw > POWER_TOL_KW:
        return Action.NO_CHARGE  # surplus exported, not stored: defer the charge
    return Action.IDLE


def solution_to_plan(
    solution: Solution,
    grid: TimeGrid,
    inputs: OptimizerInputs,
    computed_at: datetime | None = None,
) -> Plan:
    intervals: list[PlanInterval] = []
    net_battery = solution.charge_kw - solution.discharge_kw
    dt = inputs.dt_hours
    interval_cost = (
        inputs.buy * solution.grid_import_kw - inputs.sell * solution.grid_export_kw
    ) * dt
    for i, step in enumerate(grid.steps):
        intervals.append(
            PlanInterval(
                start=step.start,
                end=step.end,
                action=classify_action(
                    solution.charge_kw[i],
                    solution.discharge_kw[i],
                    inputs.pv[i],
                    solution.pv_used_kw[i],
                    inputs.load[i],
                ),
                power_kw=float(net_battery[i]),
                soc_start=float(solution.soc_kwh[i]),
                soc_end=float(solution.soc_kwh[i + 1]),
                buy=float(inputs.buy[i]),
                sell=float(inputs.sell[i]),
                pv_kw=float(inputs.pv[i]),
                load_kw=float(inputs.load[i]),
                grid_import_kw=float(solution.grid_import_kw[i]),
                grid_export_kw=float(solution.grid_export_kw[i]),
                interval_cost=float(interval_cost[i]),
            )
        )
    return Plan(
        intervals=intervals,
        objective_cost=float(np.sum(interval_cost)),
        solver_status=solution.status,
        solve_ms=solution.solve_ms,
        computed_at=computed_at or datetime.now(UTC),
    )
