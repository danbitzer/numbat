"""Reconstruct a plain-language explanation of the current action.

The MILP optimises the whole horizon and emits a schedule, not a reason — so
this is a faithful *narration of the numbers* that make step 0's action
optimal (the price's rank in the forecast, the hold value it's weighed
against, which soft levers are armed), never a claim about the solver's
internals. Everything here is derived from the finished plan plus the levers
that fed the solve, so it can't disagree with what was actually published.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from numbat.models import Action, Plan, PlanInterval

# Below this spread ($/kWh) the forecast is effectively flat, so a price's
# "rank" carries no information and the wording says so instead.
FLAT_SPREAD = 0.02


def _fmt_price(x: float) -> str:
    return f"{'−' if x < 0 else ''}${abs(x):.2f}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _local_time(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    hour = local.hour % 12 or 12
    ampm = "am" if local.hour < 12 else "pm"
    return f"{hour}:{local.minute:02d} {ampm}" if local.minute else f"{hour} {ampm}"


def _rank(values: list[float], x: float, *, descending: bool) -> int:
    """1-based rank of x (ties share the better rank): 1 = best."""
    better = sum(1 for v in values if (v > x if descending else v < x))
    return better + 1


def _next_step_with_action(plan: Plan, action: Action) -> PlanInterval | None:
    return next((iv for iv in plan.intervals[1:] if iv.action == action), None)


def _reason(
    s0: PlanInterval,
    plan: Plan,
    *,
    hold_value: float,
    sell_rank: int,
    buy_rank: int,
    n: int,
    flat: bool,
    spike_reserve: dict | None,
    daily_target_active: bool,
    live_spike: bool,
    tz: ZoneInfo,
) -> str:
    if s0.action == Action.DISCHARGE:
        if live_spike:
            return (
                f"A price spike is live — exporting stored energy at "
                f"{_fmt_price(s0.sell)}/kWh while it lasts."
            )
        where = "the highest" if sell_rank == 1 else f"the {_ordinal(sell_rank)} highest of {n}"
        tail = ""
        if s0.sell > hold_value:
            tail = (
                f", above the {_fmt_price(hold_value)}/kWh value of keeping it stored — "
                f"so selling now beats holding"
            )
        return (
            f"Exporting stored energy — the {_fmt_price(s0.sell)}/kWh feed-in price is "
            f"{where} in the forecast{tail}."
        )

    if s0.action == Action.CHARGE:
        if s0.buy < 0:
            return (
                f"Charging from the grid — the buy price is negative "
                f"({_fmt_price(s0.buy)}/kWh), so importing actually pays."
            )
        where = "the cheapest" if buy_rank == 1 else f"the {_ordinal(buy_rank)} cheapest of {n}"
        return (
            f"Charging from the grid — {_fmt_price(s0.buy)}/kWh is {where} buy price in the "
            f"forecast, banking cheap energy for a higher-value window later."
        )

    if s0.action == Action.NO_CHARGE:
        nxt = _next_step_with_action(plan, Action.CHARGE)
        when = f" (planned around {_local_time(nxt.start, tz)})" if nxt else ""
        return (
            f"Holding off charging — surplus solar is exporting at {_fmt_price(s0.sell)}/kWh "
            f"rather than filling the battery now, deferring the charge to a cheaper window{when}."
        )

    if s0.action == Action.CURTAIL:
        return (
            f"Curtailing export — the feed-in price is negative ({_fmt_price(s0.sell)}/kWh), "
            f"so spilling excess solar beats paying to export it."
        )

    # IDLE — self-consumption. Name the reason it's holding rather than trading.
    holds = []
    if spike_reserve:
        until = spike_reserve.get("until")
        by = f" by {_local_time(datetime.fromisoformat(until), tz)}" if until else ""
        holds.append(f"keeping {spike_reserve['kwh']:.0f} kWh in reserve for a possible spike{by}")
    if daily_target_active:
        holds.append("staying on track for the daily charge target")
    if flat:
        lead = (
            "Self-consumption — prices are flat across the forecast, "
            "so there's no arbitrage to chase"
        )
    else:
        lead = (
            "Self-consumption — nothing beats holding right now"
        )
    tail = (
        f"; {', '.join(holds)}"
        if holds
        else "; the battery covers the house and soaks up any solar"
    )
    return f"{lead}{tail}."


def build_explanation(
    plan: Plan,
    *,
    hold_value: float,
    price_forecast_end: datetime | None,
    spike_reserve: dict | None,
    daily_target_active: bool,
    live_spike: bool,
    prices_estimated: bool,
    capacity_kwh: float | None,
    tz: ZoneInfo,
) -> dict | None:
    if not plan.intervals:
        return None
    s0 = plan.intervals[0]
    # Rank within the REAL forecast window only — the padded tail repeats the
    # last value and would swamp the ranking with duplicates.
    horizon = [
        iv
        for iv in plan.intervals
        if price_forecast_end is None or iv.start < price_forecast_end
    ] or plan.intervals
    sells = [iv.sell for iv in horizon]
    buys = [iv.buy for iv in horizon]
    n = len(horizon)
    flat = (max(sells) - min(sells)) < FLAT_SPREAD and (max(buys) - min(buys)) < FLAT_SPREAD
    sell_rank = _rank(sells, s0.sell, descending=True)
    buy_rank = _rank(buys, s0.buy, descending=False)

    reason = _reason(
        s0,
        plan,
        hold_value=hold_value,
        sell_rank=sell_rank,
        buy_rank=buy_rank,
        n=n,
        flat=flat,
        spike_reserve=spike_reserve,
        daily_target_active=daily_target_active,
        live_spike=live_spike,
        tz=tz,
    )

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
        "reason": reason,
        "values": values,
        "context": {
            "sell_rank": sell_rank,
            "buy_rank": buy_rank,
            "horizon_steps": n,
            "hold_value": round(hold_value, 3),
            "flat": flat,
            "hysteresis": plan.solver_status.endswith("(hysteresis)"),
        },
        "levers": {
            "spike_reserve": spike_reserve,
            "daily_target": daily_target_active,
            "live_spike": live_spike,
            "prices_estimated": prices_estimated,
        },
    }
