"""The "More info" payload: step-0 facts and armed levers, no prose.

The narrated reason was removed deliberately — the decision weighs the whole
horizon (refill routes, reserves, targets, the dynamic export spread), so any
one-line comparison misled precisely on the interesting days. The panel shows
the numbers; OPTIMIZER.md explains the economics.
"""

from datetime import UTC, datetime, timedelta

from numbat.explain import build_explanation
from numbat.models import Action, Plan, PlanInterval

START = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)  # 3:30pm Adelaide


def _iv(i: int, action: Action, *, buy: float, sell: float, **kw) -> PlanInterval:
    start = START + timedelta(minutes=30 * i)
    return PlanInterval(
        start=start,
        end=start + timedelta(minutes=30),
        action=action,
        power_kw=kw.get("power_kw", 0.0),
        soc_start=kw.get("soc_start", 20.0),
        soc_end=kw.get("soc_end", 20.0),
        buy=buy,
        sell=sell,
        pv_kw=kw.get("pv_kw", 0.0),
        load_kw=kw.get("load_kw", 0.5),
        grid_import_kw=kw.get("grid_import_kw", 0.0),
        grid_export_kw=kw.get("grid_export_kw", 0.0),
        interval_cost=kw.get("interval_cost", 0.0),
    )


def _plan(intervals, status="optimal") -> Plan:
    return Plan(
        intervals=intervals,
        objective_cost=0.0,
        solver_status=status,
        solve_ms=1.0,
        computed_at=START,
    )


def _build(plan, **overrides):
    kw = dict(
        hold_value=0.20,
        spike_reserve=None,
        daily_target_active=False,
        live_spike=False,
        prices_estimated=False,
        capacity_kwh=44.8,
    )
    kw.update(overrides)
    return build_explanation(plan, **kw)


def test_carries_the_step0_numbers_and_no_prose():
    intervals = [
        _iv(0, Action.DISCHARGE, buy=0.90, sell=0.85, power_kw=-8.0, grid_export_kw=8.0,
            interval_cost=-0.34, soc_start=38.0, soc_end=34.0),
        _iv(1, Action.IDLE, buy=0.30, sell=0.25),
    ]
    exp = _build(_plan(intervals))
    assert exp is not None
    assert "reason" not in exp  # the one-liner is gone, deliberately
    v = exp["values"]
    assert v["sell"] == 0.85
    assert v["battery_kw"] == -8.0
    assert v["grid_export_kw"] == 8.0
    assert v["interval_cost"] == -0.34
    assert v["soc_start_pct"] == round(100 * 38.0 / 44.8, 1)
    assert v["soc_end_pct"] == round(100 * 34.0 / 44.8, 1)
    assert exp["context"]["hold_value"] == 0.20


def test_soc_percentages_omitted_without_capacity():
    exp = _build(_plan([_iv(0, Action.IDLE, buy=0.30, sell=0.25)]), capacity_kwh=None)
    assert "soc_start_pct" not in exp["values"]


def test_levers_reflect_what_armed_the_solve():
    # the shape api.ts requires: {soc, threshold, released}
    reserve = {"soc": 0.3, "threshold": 1.0, "released": False}
    exp = _build(
        _plan([_iv(0, Action.IDLE, buy=0.30, sell=0.25)]),
        spike_reserve=reserve,
        daily_target_active=True,
        live_spike=True,
        prices_estimated=True,
    )
    assert exp["levers"] == {
        "spike_reserve": reserve,
        "daily_target": True,
        "live_spike": True,
        "prices_estimated": True,
    }


def test_hysteresis_flag_surfaced():
    intervals = [
        _iv(0, Action.IDLE, buy=0.30, sell=0.25),
        _iv(1, Action.IDLE, buy=0.30, sell=0.25),
    ]
    exp = _build(_plan(intervals, status="optimal (hysteresis)"))
    assert exp["context"]["hysteresis"] is True


def test_empty_plan_returns_none():
    assert _build(_plan([])) is None
