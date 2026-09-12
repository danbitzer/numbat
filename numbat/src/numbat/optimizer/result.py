"""Map a raw MILP solution onto the Plan domain model."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from numbat.models import Action, Plan, PlanInterval
from numbat.optimizer.model import OptimizerInputs, Solution
from numbat.timegrid import TimeGrid

POWER_TOL_KW = 0.01
CURTAIL_TOL_KW = 0.05


def hold_floor_kwh(soc_min_kwh: float, capacity_kwh: float) -> float:
    """SoC at/below which HOLD stops being classified.

    The margin must clear SoC sensor quantization: a 1%-granularity sensor
    on a 12.8 kWh pack steps 0.128 kWh, and a smaller margin flaps
    hold<->idle on single-percent jitter (each flap actuates limit writes).
    Two percent of capacity clears two sensor steps. Assumes the inverter's
    own reserve sits at/above soc_min: a battery honestly BELOW the floor
    (BMS recalibration) classifies IDLE, whose self-consumption actuation
    may drain to the inverter's own reserve — fine when that reserve is
    the same floor, which is the typical mirrored configuration."""
    return soc_min_kwh + max(0.1, 0.02 * capacity_kwh)


def charge_ceiling_kwh(soc_max_kwh: float, capacity_kwh: float) -> float:
    """SoC at/above which the battery counts as FULL for classification (the
    mirror of hold_floor_kwh, same sensor-quantization margin): a battery
    with less room than this can't usefully take a charge, so solar the plan
    spills next to it is CURTAIL (nowhere to put it), not NO_CHARGE (a
    decision to keep the room)."""
    return soc_max_kwh - max(0.1, 0.02 * capacity_kwh)


def classify_action(
    charge_kw: float,
    discharge_kw: float,
    pv_kw: float,
    pv_used_kw: float,
    load_kw: float,
    holdable: bool = True,
    chargeable: bool = True,
) -> Action:
    """Grid-coupled semantics: charge/discharge are reserved for battery moves
    a self-consumption inverter mode would NOT make on its own.

    - DISCHARGE: battery power beyond the house's unmet load — i.e. exporting
      stored energy (forced discharge is the right actuation: a pinned high
      setpoint, not load-following).
    - CHARGE: charging beyond the PV surplus — i.e. buying from the grid.
    - HOLD: battery inert while the GRID serves the house — the plan prefers
      importing over spending stored energy, but self-consumption would
      discharge into the load. Checked before CURTAIL: on a negative-price
      interval both can be true, and guarding the battery is the part the
      action must carry (the export cap rides the orthogonal `curtail`
      attribute). `holdable` gates it: a battery at its floor has nothing
      worth holding, so that case stays IDLE.
    - NO_CHARGE: the battery is not charging although there is PV surplus it
      could take and room to take it — i.e. self-consumption WOULD charge,
      but the plan keeps the room (a cheaper or paid fill later: a negative
      buy price, or a better solar window). The surplus is exported or, at
      negative feed-in, spilled under the `curtail` attribute — either way
      the actuation is "block charging, still cover load dips"; plain
      self-consumption + cap would fill the battery and defeat the plan
      (seen in a 2026-09-10 replay: draining into the house and spilling
      8.9 kW at −8c to be paid 15c to refill from the grid at 12:30).
      `chargeable` gates it: a full battery spilling solar is CURTAIL.
      Checked after HOLD (the stronger fence) and before CURTAIL.
    - CURTAIL: PV spilled with nowhere to put it — the battery is full or
      already charging at its maximum.
    - IDLE: everything else self-consumption-shaped (running the house off the
      battery, charging from excess PV) — the inverter's native mode does
      this with second-by-second load tracking a 5-min setpoint can't match.
    """
    export_discharge = discharge_kw - max(load_kw - pv_used_kw, 0.0)
    grid_charge = charge_kw - max(pv_used_kw - load_kw, 0.0)
    if export_discharge > POWER_TOL_KW:
        return Action.DISCHARGE
    if grid_charge > POWER_TOL_KW:
        return Action.CHARGE
    battery_inactive = charge_kw <= POWER_TOL_KW and discharge_kw <= POWER_TOL_KW
    if (
        holdable
        and battery_inactive
        and load_kw > POWER_TOL_KW
        and pv_used_kw < load_kw - POWER_TOL_KW
    ):
        return Action.HOLD  # grid serves the house; keep the battery out of it
    surplus = pv_kw - load_kw > POWER_TOL_KW
    if chargeable and charge_kw <= POWER_TOL_KW and surplus:
        return Action.NO_CHARGE  # room and surplus, yet not charging: keep the room
    if pv_kw > CURTAIL_TOL_KW and pv_used_kw < pv_kw - CURTAIL_TOL_KW:
        return Action.CURTAIL
    return Action.IDLE


def solution_to_plan(
    solution: Solution,
    grid: TimeGrid,
    inputs: OptimizerInputs,
    computed_at: datetime | None = None,
    hold_floor_kwh: float = 0.0,
    charge_ceiling_kwh: float = float("inf"),
) -> Plan:
    """hold_floor_kwh: SoC at/below which HOLD stops being classified (the
    battery floor plus a small margin) — holding an empty battery is noise.
    charge_ceiling_kwh: SoC at/above which NO_CHARGE stops being classified
    (the battery is full: spilled solar is CURTAIL, not a kept room)."""
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
                    holdable=float(solution.soc_kwh[i]) > hold_floor_kwh,
                    chargeable=float(solution.soc_kwh[i]) < charge_ceiling_kwh,
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
                pv_used_kw=float(solution.pv_used_kw[i]),
            )
        )
    return Plan(
        intervals=intervals,
        objective_cost=float(np.sum(interval_cost)),
        solver_status=solution.status,
        solve_ms=solution.solve_ms,
        computed_at=computed_at or datetime.now(UTC),
    )
