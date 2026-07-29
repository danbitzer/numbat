"""Test mode: run the optimizer against hand-picked synthetic Amber price
scenarios so users can see how Numbat responds without waiting for real data.

Pure and side-effect-free: builds synthetic inputs, runs the same solver the
live loop uses, and returns a plan in the /api/plan shape. It never publishes
sensors, touches /data, or mutates the live planner — safe to call anytime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from numbat.config import Settings
from numbat.explain import build_explanation
from numbat.optimizer.model import (
    GridParams,
    OptimizerConfig,
    OptimizerInputs,
    auto_terminal_value,
    solve,
)
from numbat.optimizer.result import solution_to_plan
from numbat.planner import (
    battery_params,
    daily_soc_target_vector,
    haircut_sell,
    spike_reserve_vector,
)
from numbat.timegrid import TimeGrid

# generate(hours, days) -> (buy, sell, pv, load) arrays over the grid.
Generator = Callable[
    [np.ndarray, np.ndarray],
    tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
]


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    description: str
    # `hours` (arg 1) is local hour-of-day (0-24, fractional); `days` (arg 2) is
    # the integer day offset from the first step (0 = today, 1 = tomorrow, ...).
    generate: Generator


def _bell(hours: np.ndarray, peak: float, start: float = 8.0, end: float = 16.0) -> np.ndarray:
    """A daytime solar bell (0 outside [start,end], sinusoidal peak between)."""
    x = np.clip((hours - start) / (end - start), 0.0, 1.0)
    return np.where((hours >= start) & (hours < end), peak * np.sin(x * np.pi), 0.0)


def _house_load(hours: np.ndarray) -> np.ndarray:
    """A gentle household load: ~0.4 kW base with morning and evening bumps."""
    load = np.full_like(hours, 0.4)
    load += np.where((hours >= 6) & (hours < 9), 0.6, 0.0)     # morning
    load += np.where((hours >= 17) & (hours < 22), 0.9, 0.0)   # evening
    return load


def _evening_peak_buy(hours: np.ndarray) -> np.ndarray:
    """A typical import curve: cheap overnight, mild midday, evening peak."""
    buy = np.full_like(hours, 0.15)
    buy = np.where((hours >= 0) & (hours < 6), 0.10, buy)      # overnight
    buy = np.where((hours >= 10) & (hours < 15), 0.12, buy)    # solar-soft midday
    buy = np.where((hours >= 17) & (hours < 20), 0.40, buy)    # evening peak
    return buy


# --- scenario generators -------------------------------------------------

def _typical(hours, days):
    buy = _evening_peak_buy(hours)
    sell = np.clip(buy - 0.10, -0.02, None)
    return buy, sell, _bell(hours, 6.0), _house_load(hours)


def _evening_spike(hours, days):
    buy = _evening_peak_buy(hours)
    sell = np.clip(buy - 0.10, -0.02, None)
    # A sharp wholesale spike this evening (today only), 6-8pm.
    spike = (days == 0) & (hours >= 18) & (hours < 20)
    buy = np.where(spike, 1.40, buy)
    sell = np.where(spike, 1.25, sell)
    return buy, sell, _bell(hours, 6.0), _house_load(hours)


def _export_spike_tonight(hours, days):
    # High feed-in tonight (a sell spike, buy stays moderate) — worth pre-charging
    # and exporting; then normal.
    buy = _evening_peak_buy(hours)
    sell = np.clip(buy - 0.10, -0.02, None)
    win = (days == 0) & (hours >= 18) & (hours < 20)
    sell = np.where(win, 0.90, sell)
    return buy, sell, _bell(hours, 6.0), _house_load(hours)


def _negative_feedin_tomorrow(hours, days):
    # Solar glut tomorrow: feed-in goes negative midday (you pay to export),
    # after a normal evening tonight. Tests curtailment + the "dump tonight,
    # refill from free PV tomorrow" behaviour.
    buy = _evening_peak_buy(hours)
    sell = np.clip(buy - 0.10, -0.02, None)
    glut = (days == 1) & (hours >= 9) & (hours < 16)
    sell = np.where(glut, -0.06, sell)
    buy = np.where(glut, 0.03, buy)
    pv = _bell(hours, 6.0)
    pv = np.where(days == 1, _bell(hours, 11.0), pv)  # big sunny day tomorrow
    return buy, sell, pv, _house_load(hours)


def _low_morning_rising_afternoon(hours, days):
    # Prices low in the morning, climbing into the afternoon/evening.
    buy = 0.08 + 0.02 * np.clip(hours - 6, 0, 12)   # ramps from 8c at 6am
    buy = np.where(hours < 6, 0.10, buy)
    sell = np.clip(buy - 0.08, -0.02, None)
    return buy, sell, _bell(hours, 6.0), _house_load(hours)


def _flat(hours, days):
    buy = np.full_like(hours, 0.25)
    sell = np.full_like(hours, 0.12)
    return buy, sell, _bell(hours, 6.0), _house_load(hours)


def _cheap_overnight_charge(hours, days):
    # Very cheap (even negative) overnight, expensive evening — classic
    # "charge overnight, discharge into the evening peak".
    buy = _evening_peak_buy(hours)
    buy = np.where((hours >= 1) & (hours < 5), -0.02, buy)
    sell = np.clip(buy - 0.10, -0.05, None)
    return buy, sell, _bell(hours, 3.0), _house_load(hours)  # dull solar day


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in [
        Scenario("typical", "Typical day",
                 "Cheap overnight, mild midday, an evening import peak, modest feed-in.",
                 _typical),
        Scenario("evening_spike", "Price spike tonight",
                 "A sharp wholesale spike this evening (6-8pm) — buy and feed-in both jump.",
                 _evening_spike),
        Scenario("export_spike_tonight", "Feed-in spike tonight",
                 "Feed-in spikes tonight while import stays moderate — worth pre-charging to sell.",
                 _export_spike_tonight),
        Scenario("negative_feedin_tomorrow", "Negative feed-in tomorrow",
                 "Normal tonight, then a solar glut tomorrow turns midday feed-in negative.",
                 _negative_feedin_tomorrow),
        Scenario("low_morning_rising", "Low morning, rising afternoon",
                 "Prices start low in the morning and climb into the afternoon and evening.",
                 _low_morning_rising_afternoon),
        Scenario("cheap_overnight", "Cheap (negative) overnight",
                 "Very cheap — even negative — overnight, a pricey evening, and a dull solar day.",
                 _cheap_overnight_charge),
        Scenario("flat", "Flat prices",
                 "Import and feed-in flat all horizon — no arbitrage to chase.",
                 _flat),
    ]
}


def scenario_list() -> list[dict]:
    return [
        {"id": s.id, "label": s.label, "description": s.description}
        for s in SCENARIOS.values()
    ]


def run_simulation(
    settings: Settings,
    *,
    scenario_id: str,
    soc_frac: float,
    now: datetime,
    tz: ZoneInfo,
) -> dict:
    if scenario_id not in SCENARIOS:
        raise KeyError(scenario_id)
    scenario = SCENARIOS[scenario_id]

    horizon_h = settings.optimizer.horizon_hours
    horizon = timedelta(hours=horizon_h)
    # A clean uniform 30-minute grid over the horizon (no live forecast to align
    # to — the scenario defines every step).
    boundaries = [now + timedelta(minutes=30 * k) for k in range(1, horizon_h * 2 + 2)]
    grid = TimeGrid.build(now, boundaries, horizon)

    starts = [s.start.astimezone(tz) for s in grid.steps]
    day0 = starts[0].date()
    hours = np.array([t.hour + t.minute / 60 for t in starts])
    days = np.array([(t.date() - day0).days for t in starts])
    buy, sell, pv, load = (np.asarray(a, dtype=float) for a in scenario.generate(hours, days))

    return simulate_solve(
        settings,
        grid=grid,
        buy=buy,
        sell=sell,
        pv=pv,
        load=load,
        soc_frac=soc_frac,
        tz=tz,
        meta_extra={"scenario": scenario_id},
    )


def simulate_solve(
    settings: Settings,
    *,
    grid: TimeGrid,
    buy: np.ndarray,
    sell: np.ndarray,
    pv: np.ndarray,
    load: np.ndarray,
    soc_frac: float,
    tz: ZoneInfo,
    meta_extra: dict | None = None,
) -> dict:
    """The shared test-mode core: arm the same soft levers the live planner
    uses, solve, and shape a /api/plan-compatible response. Pure CPU — callers
    own where the input arrays came from (synthetic scenario or recorded
    history) and which settings apply (live, or the test-mode sandbox merged
    over live by the endpoint)."""
    now = grid.start

    bp = battery_params(settings)
    grid_params = GridParams(
        import_limit_kw=settings.grid.import_limit_kw,
        export_limit_kw=settings.grid.export_limit_kw,
        min_battery_export_price=settings.grid.min_battery_export_price,
    )

    target = daily_soc_target_vector(
        grid, tz,
        target_soc=settings.battery.daily_target_soc,
        target_time=settings.battery.daily_target_time,
        hold_hours=settings.battery.daily_target_hold_hours,
        capacity_kwh=bp.capacity_kwh,
        soc_max_kwh=bp.soc_max_kwh,
    )
    reserve = spike_reserve_vector(
        sell, grid.dt_hours,
        lookahead_hours=settings.spike.lookahead_hours,
        high_price_threshold=settings.spike.high_price_threshold,
        reserve_kwh=settings.spike.reserve_kwh,
        soc_max_kwh=bp.soc_max_kwh,
    )

    # Same forecast skepticism as the live planner: the haircut shapes the
    # solve (step 0 — the scenario's "current" price, or the recorded instant
    # in time travel — is exempt), while the reported plan quotes the raw
    # prices. Without this the sandbox's haircut knob would silently do
    # nothing, making test-mode A/B runs lie.
    sell_solve = haircut_sell(sell, settings.optimizer.forecast_haircut)

    inputs = OptimizerInputs(
        dt_hours=grid.dt_hours,
        buy=buy, sell=sell_solve, pv=pv, load=load,
        soc0_kwh=float(np.clip(soc_frac, 0.0, 1.0)) * bp.capacity_kwh,
        reserve_kwh=reserve,
        soc_target_kwh=target,
    )

    cfg = settings.optimizer
    # The scenario fills every step, so there is no padded tail to exclude here.
    terminal = (
        auto_terminal_value(buy, bp, floor=cfg.hold_value_floor, scaling=cfg.hold_value_scaling)
        if cfg.terminal_soc_value == "auto"
        else float(cfg.terminal_soc_value)
    )
    # Same tariff-tracking lift the live planner applies (every sim step is a
    # real price — no padded tail to exclude).
    penalty = settings.battery.daily_target_penalty_per_kwh
    if settings.battery.daily_target_penalty_price_multiple > 0 and buy.size:
        penalty = max(
            penalty,
            settings.battery.daily_target_penalty_price_multiple * float(np.median(buy)),
        )
    opt_config = OptimizerConfig(
        terminal_value=terminal,
        reserve_penalty_per_kwh=settings.spike.reserve_penalty_per_kwh,
        solver_timeout_s=cfg.solver_timeout_s,
        soc_target_penalty_per_kwh=penalty,
        min_battery_export_spread=cfg.min_battery_export_spread,
        import_penalty_per_kwh=cfg.import_penalty_per_kwh,
    )

    solution = solve(inputs, bp, grid_params, opt_config)
    plan = solution_to_plan(solution, grid, replace(inputs, sell=sell), computed_at=now)
    plan.explanation = build_explanation(
        plan,
        hold_value=terminal,
        spike_reserve=(
            {"kwh": float(reserve[0]), "until": None} if reserve is not None else None
        ),
        daily_target_active=target is not None,
        live_spike=False,
        prices_estimated=False,
        capacity_kwh=bp.capacity_kwh,
    )

    return {
        "computed_at": plan.computed_at.isoformat(),
        "solver_status": plan.solver_status,
        "solve_ms": plan.solve_ms,
        "objective_cost": plan.objective_cost,
        "meta": {
            "capacity_kwh": bp.capacity_kwh,
            "price_forecast_end": grid.end.isoformat(),
            "load_forecast": "learned",
            "simulated": True,
            "explanation": plan.explanation,
            **(meta_extra or {}),
        },
        "intervals": [
            {
                "start": iv.start.isoformat(),
                "end": iv.end.isoformat(),
                "action": iv.action.value,
                "power_kw": iv.power_kw,
                "soc_start": iv.soc_start,
                "soc_end": iv.soc_end,
                "buy": iv.buy,
                "sell": iv.sell,
                "pv_kw": iv.pv_kw,
                "load_kw": iv.load_kw,
                "grid_import_kw": iv.grid_import_kw,
                "grid_export_kw": iv.grid_export_kw,
                "interval_cost": iv.interval_cost,
            }
            for iv in plan.intervals
        ],
    }
