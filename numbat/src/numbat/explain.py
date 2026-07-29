"""Assemble the step-0 facts behind the current action for the dashboard.

The MILP optimises the whole horizon and emits a schedule, not a reason.
Numbat used to narrate one anyway ("…beats the hold value, so selling now
beats holding") — but any one-liner compares against a single quantity,
while the real decision weighs the entire horizon: refill routes, reserves,
targets, the dynamic export spread. There is no honest sentence, so the
"More info" panel shows the numbers and the armed levers instead, and
OPTIMIZER.md explains the economics. Everything here is copied from the
finished plan plus the levers that fed the solve, so it can't disagree with
what was actually published.
"""

from __future__ import annotations

from numbat.models import Plan


def build_explanation(
    plan: Plan,
    *,
    hold_value: float,
    spike_reserve: dict | None,
    daily_target_active: bool,
    live_spike: bool,
    prices_estimated: bool,
    capacity_kwh: float | None,
) -> dict | None:
    if not plan.intervals:
        return None
    s0 = plan.intervals[0]

    values: dict = {
        "buy": s0.buy,
        "sell": s0.sell,
        "pv_kw": s0.pv_kw,
        "load_kw": s0.load_kw,
        "soc_start_kwh": round(s0.soc_start, 2),
        "soc_end_kwh": round(s0.soc_end, 2),
        "battery_kw": s0.power_kw,
        "grid_import_kw": s0.grid_import_kw,
        "grid_export_kw": s0.grid_export_kw,
        "interval_cost": s0.interval_cost,
    }
    if capacity_kwh:
        values["soc_start_pct"] = round(100 * s0.soc_start / capacity_kwh, 1)
        values["soc_end_pct"] = round(100 * s0.soc_end / capacity_kwh, 1)

    return {
        "values": values,
        "context": {
            "hold_value": round(hold_value, 3),
            "hysteresis": plan.solver_status.endswith("(hysteresis)"),
        },
        "levers": {
            "spike_reserve": spike_reserve,
            "daily_target": daily_target_active,
            "live_spike": live_spike,
            "prices_estimated": prices_estimated,
        },
    }
