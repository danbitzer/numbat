"""The 'why this action' explanation is a faithful narration of the plan."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from numbat.explain import build_explanation
from numbat.models import Action, Plan, PlanInterval

ADELAIDE = ZoneInfo("Australia/Adelaide")
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
        price_forecast_end=None,
        spike_reserve=None,
        daily_target_active=False,
        live_spike=False,
        prices_estimated=False,
        capacity_kwh=44.8,
        tz=ADELAIDE,
    )
    kw.update(overrides)
    return build_explanation(plan, **kw)


def test_discharge_is_top_ranked_sell_above_hold():
    # step 0 sells at the horizon's highest feed-in price, above hold value
    intervals = [
        _iv(0, Action.DISCHARGE, buy=0.90, sell=0.85, power_kw=-8.0, grid_export_kw=8.0,
            interval_cost=-0.34, soc_start=38.0, soc_end=34.0),
        _iv(1, Action.IDLE, buy=0.30, sell=0.25),
        _iv(2, Action.IDLE, buy=0.20, sell=0.15),
    ]
    exp = _build(_plan(intervals))
    assert exp is not None
    assert "Exporting stored energy" in exp["reason"]
    assert "highest" in exp["reason"]
    assert "$0.20/kWh" in exp["reason"]  # the hold value it beats
    assert exp["context"]["sell_rank"] == 1
    assert exp["context"]["horizon_steps"] == 3
    assert exp["values"]["grid_export_kw"] == 8.0
    assert exp["values"]["soc_start_pct"] == round(100 * 38.0 / 44.8, 1)


def test_discharge_during_live_spike_says_so():
    intervals = [
        _iv(0, Action.DISCHARGE, buy=1.20, sell=1.10, grid_export_kw=8.0),
        _iv(1, Action.IDLE, buy=0.30, sell=0.25),
    ]
    exp = _build(_plan(intervals), live_spike=True)
    assert "spike is live" in exp["reason"]
    assert exp["levers"]["live_spike"] is True


def test_charge_on_negative_price():
    intervals = [
        _iv(0, Action.CHARGE, buy=-0.05, sell=-0.10, power_kw=6.0, grid_import_kw=6.0),
        _iv(1, Action.IDLE, buy=0.30, sell=0.25),
    ]
    exp = _build(_plan(intervals))
    assert "negative" in exp["reason"]
    assert "importing actually pays" in exp["reason"]


def test_charge_cheapest_window():
    intervals = [
        _iv(0, Action.CHARGE, buy=0.10, sell=0.05, grid_import_kw=6.0),
        _iv(1, Action.IDLE, buy=0.30, sell=0.25),
        _iv(2, Action.DISCHARGE, buy=0.80, sell=0.75),
    ]
    exp = _build(_plan(intervals))
    assert "Charging from the grid" in exp["reason"]
    assert "cheapest" in exp["reason"]
    assert exp["context"]["buy_rank"] == 1


def test_curtail_on_negative_feed_in():
    intervals = [
        _iv(0, Action.CURTAIL, buy=0.20, sell=-0.08, pv_kw=6.0),
        _iv(1, Action.IDLE, buy=0.20, sell=0.15),
    ]
    exp = _build(_plan(intervals))
    assert "Curtailing export" in exp["reason"]
    assert "negative" in exp["reason"]


def test_idle_names_the_spike_reserve_being_held():
    intervals = [
        _iv(0, Action.IDLE, buy=0.30, sell=0.25),
        _iv(1, Action.IDLE, buy=0.35, sell=0.30),
    ]
    reserve = {"kwh": 22.0, "until": (START + timedelta(hours=3)).isoformat()}
    exp = _build(_plan(intervals), spike_reserve=reserve)
    assert "reserve" in exp["reason"]
    assert "22 kWh" in exp["reason"]
    assert exp["levers"]["spike_reserve"]["kwh"] == 22.0


def test_flat_prices_wording():
    intervals = [_iv(i, Action.IDLE, buy=0.25, sell=0.20) for i in range(4)]
    exp = _build(_plan(intervals))
    assert "flat" in exp["reason"]
    assert exp["context"]["flat"] is True


def test_hysteresis_flag_surfaced():
    intervals = [
        _iv(0, Action.IDLE, buy=0.30, sell=0.25),
        _iv(1, Action.IDLE, buy=0.30, sell=0.25),
    ]
    exp = _build(_plan(intervals, status="optimal (hysteresis)"))
    assert exp["context"]["hysteresis"] is True


def test_ranking_ignores_padded_tail():
    # A real window of 2 steps, then a long padded tail repeating a high sell.
    # step 0's sell should still rank against only the real window.
    forecast_end = START + timedelta(hours=1)
    intervals = [
        _iv(0, Action.DISCHARGE, buy=0.90, sell=0.85, grid_export_kw=8.0),
        _iv(1, Action.IDLE, buy=0.30, sell=0.25),
        *[_iv(i, Action.IDLE, buy=0.99, sell=0.95) for i in range(2, 40)],
    ]
    exp = _build(_plan(intervals), price_forecast_end=forecast_end)
    assert exp["context"]["horizon_steps"] == 2
    assert exp["context"]["sell_rank"] == 1


def test_empty_plan_returns_none():
    assert _build(_plan([])) is None
