"""The flow vocabulary: what the energy is doing this interval, in the words a
householder would use.

The action sensor speaks to automations ("charge" = forced charge from the
grid, "idle" = self-consumption, …) and that contract is frozen. But "idle"
is the busiest, most valuable mode — solar filling the battery, the battery
running the house — and it reads as "the add-on has stopped". So the
dashboard shows a FLOW instead, derived here from the plan, ACTION-FIRST:
the action decides the family, the numbers only refine within self-
consumption, so the tile can never disagree with the instruction that was
actually published. Two orthogonal modifiers ride every interval — export
capped and PV off — plus the solar the plan throws away.

Published as additive attributes on sensor.numbat_action (`flow`,
`pv_spill_kw`) and per interval in /api/plan; nothing existing is renamed.
"""

from __future__ import annotations

from numbat.models import Action, Plan, PlanInterval
from numbat.optimizer.result import CURTAIL_TOL_KW, POWER_TOL_KW

# Enter PV-off only once the live buy price is at least this negative (see
# planner.pv_off_wanted). Lives here because the per-interval display flag
# uses the same threshold against forecast prices.
PV_OFF_ENTRY_BUY = -0.01  # $/kWh

FLOWS = (
    "charging_from_grid",  # action charge: forced charge, grid-side kW beyond any solar
    "selling_stored_energy",  # action discharge: forced discharge to the grid
    "running_on_grid",  # action hold: battery fenced, the house imports
    "holding_back_solar",  # action curtail: solar spilled, export capped
    "selling_solar",  # action no_charge, or idle with the surplus exported (battery full)
    "storing_solar",  # idle, battery charging from the surplus
    "running_on_battery",  # idle, battery discharging into the house
    "solar_running_house",  # idle, solar covers the house, battery untouched
    "battery_empty",  # idle at the floor: the house imports, nothing to spend
    "waiting",  # nothing flowing at all
)


def flow_for(iv: PlanInterval) -> str:
    """The flow of one interval. Action-first; numbers only refine `idle`."""
    match iv.action:
        case Action.CHARGE:
            return "charging_from_grid"
        case Action.DISCHARGE:
            return "selling_stored_energy"
        case Action.HOLD:
            return "running_on_grid"
        case Action.CURTAIL:
            return "holding_back_solar"
        case Action.NO_CHARGE:
            return "selling_solar"
    # idle: self-consumption, whatever the inverter does with it
    if iv.power_kw > POWER_TOL_KW:
        return "storing_solar"
    if iv.grid_export_kw > CURTAIL_TOL_KW:
        return "selling_solar"
    if iv.power_kw < -POWER_TOL_KW:
        return "running_on_battery"
    if iv.pv_used_kw > POWER_TOL_KW and iv.pv_used_kw >= iv.load_kw - POWER_TOL_KW:
        return "solar_running_house"
    if iv.grid_import_kw > POWER_TOL_KW:
        return "battery_empty"
    return "waiting"


def annotate(plan: Plan) -> Plan:
    """Fill every interval's flow, modifiers and spill. Step 0's modifiers
    are the plan's live-price-gated flags (what was published); later steps
    apply the planner's thresholds to the forecast prices — for PV-off as a
    forward pass with the same asymmetry (a cent below zero to enter, any
    negative price to stay), so the strip shows the runs the actuator would
    hold, not a gap at every shallow dip. The estimate hold has no forecast
    analogue."""
    pv_off = plan.pv_off
    for i, iv in enumerate(plan.intervals):
        spill = iv.pv_kw - iv.pv_used_kw
        iv.pv_spill_kw = round(spill, 2) if spill > CURTAIL_TOL_KW else 0.0
        if i == 0:
            iv.export_capped = plan.curtail_export
            iv.pv_off = plan.pv_off
        else:
            iv.export_capped = (
                iv.sell < 0 and iv.grid_export_kw < CURTAIL_TOL_KW and iv.pv_kw > POWER_TOL_KW
            )
            unused_pv = iv.pv_kw > POWER_TOL_KW and iv.pv_used_kw < POWER_TOL_KW
            pv_off = unused_pv and iv.buy < (0.0 if pv_off else PV_OFF_ENTRY_BUY)
            iv.pv_off = pv_off
        iv.flow = flow_for(iv)
    return plan


def details(plan: Plan, capacity_kwh: float | None) -> dict | None:
    """Step 0's flow plus the look-ahead facts the tile's sub-label quotes:
    when the battery next fills (and from what), when it is next spent (and
    the buy price then), and how low it goes over the next 12 hours."""
    if not plan.intervals:
        return None
    s0 = plan.intervals[0]
    out: dict = {
        "key": s0.flow,
        "export_capped": s0.export_capped,
        "pv_off": s0.pv_off,
        "pv_spill_kw": s0.pv_spill_kw,
    }
    later = plan.intervals[1:]
    fill = next((iv for iv in later if iv.power_kw > POWER_TOL_KW), None)
    if fill is not None:
        out["next_fill_time"] = fill.start.isoformat()
        out["next_fill_source"] = "grid" if fill.action == Action.CHARGE else "solar"
    use = next((iv for iv in later if iv.power_kw < -POWER_TOL_KW), None)
    if use is not None:
        # what the stored energy is next spent on: the house (the buy price
        # then is the saving) or a forced export (the feed-in price then is
        # the earning) — the tile quotes whichever applies
        export = use.action == Action.DISCHARGE
        out["next_use_time"] = use.start.isoformat()
        out["next_use_kind"] = "export" if export else "house"
        out["next_use_price"] = use.sell if export else use.buy
        out["next_use_buy"] = use.buy  # kept for older frontends
    if capacity_kwh:
        window_s = 12 * 3600
        horizon = s0.start.timestamp() + window_s
        # only claim "over the next 12 h" when the plan actually covers it
        # (a fallback plan shrinks every cycle)
        if plan.intervals[-1].end.timestamp() >= horizon:
            ahead = [iv.soc_end for iv in plan.intervals if iv.start.timestamp() < horizon]
            out["soc_min_ahead_pct"] = round(100 * min(ahead) / capacity_kwh, 1)
    return out
