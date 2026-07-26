"""One optimization cycle: gather -> normalize -> solve -> Plan.

Also owns the judgment calls around the raw MILP:
- staleness policy (degraded inputs must never silently produce a plan)
- step-0 price override with the live 5-min prices
- forecast haircut (distant sell prices discounted toward the median)
- spike reserve trigger (soft SoC floor while a potential spike is ahead)
- hysteresis (pin-and-compare before switching the current action)
- live-spike guard (never grid-charge during a confirmed spike)
- fallback (reuse the previous plan when the solver fails)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import numpy as np

from numbat.adapters.amber import PriceProvider
from numbat.adapters.solar import OpenMeteoSolarAdapter
from numbat.adapters.sungrow import SungrowAdapter
from numbat.adapters.weather import WeatherAdapter
from numbat.config import Settings
from numbat.explain import build_explanation
from numbat.forecast.load import LoadForecaster
from numbat.models import Action, BatteryState, Plan, PriceForecast, Series
from numbat.optimizer.model import (
    BatteryParams,
    GridParams,
    OptimizerConfig,
    OptimizerInputs,
    SolverError,
    auto_terminal_value,
    solve,
)
from numbat.optimizer.result import classify_action, solution_to_plan
from numbat.timegrid import TimeGrid, coverage, resample_mean, resample_previous

log = logging.getLogger(__name__)

MAX_PRICE_AGE = timedelta(minutes=15)
MAX_SOC_AGE = timedelta(minutes=10)
HAIRCUT_START = timedelta(hours=6)


class InputsStale(Exception):
    pass


def spike_reserve_vector(
    sell: np.ndarray,
    dt_hours: np.ndarray,
    *,
    lookahead_hours: float,
    high_price_threshold: float,
    reserve_kwh: float,
    soc_max_kwh: float,
) -> np.ndarray | None:
    """Soft SoC floor up to the first high-price step within the lookahead
    window, so energy is held ready to sell into a potential spike."""
    if reserve_kwh <= 0:
        return None
    offset = 0.0  # hours from now to the step's start
    trigger = None
    for i, dt in enumerate(dt_hours):
        if offset > lookahead_hours:
            break
        if sell[i] >= high_price_threshold:
            trigger = i
            break
        offset += float(dt)
    if trigger is None or trigger == 0:
        return None  # no potential spike ahead, or it's already here — sell, don't hold
    reserve = np.zeros(len(dt_hours))
    reserve[:trigger] = min(reserve_kwh, soc_max_kwh)
    return reserve


def daily_soc_target_vector(
    grid: TimeGrid,
    tz: ZoneInfo,
    *,
    target_soc: float,
    target_time: dt_time,
    hold_hours: float,
    capacity_kwh: float,
    soc_max_kwh: float | None = None,
) -> np.ndarray | None:
    """Soft daily SoC target as a windowed FLOOR (length T, aligned with
    soc[1:] — the reserve convention): from each local `target_time` inside the
    horizon and for the next `hold_hours`, require target_soc×capacity.

    The daily full-charge insurance: unforecast spikes and surprise load have
    zero value in the objective, so pure economics stop charging at "enough for
    the forecast" — this prices being full THROUGH the evening peak, not just at
    a single instant it can dump the moment after. hold_hours=0 collapses to the
    single step containing the target time. Steps only (soc[0] is fixed).
    """
    if target_soc <= 0:
        return None
    # soc[1:] is the SoC at each step's END; target[i] constrains soc[i+1].
    ends = [s.end for s in grid.steps]
    target = np.zeros(len(grid.steps))
    kwh = target_soc * capacity_kwh
    if soc_max_kwh is not None:
        # clamp like the spike reserve: a target above soc_max would bake an
        # unavoidable phantom penalty into every objective
        kwh = min(kwh, soc_max_kwh)
    day = ends[0].astimezone(tz).date()
    last_day = ends[-1].astimezone(tz).date()
    while day <= last_day:
        # DST note: a nonexistent/ambiguous local time (spring-forward gap,
        # fall-back repeat — only 2-3am in AU) resolves via fold=0 to the
        # sane neighbor; no special handling needed.
        win_start = datetime.combine(day, target_time, tzinfo=tz)
        win_end = win_start + timedelta(hours=hold_hours)
        # Skip a window that has already fully elapsed before the grid starts —
        # otherwise the arrival fallback below would pin step 0 spuriously (its
        # end is trivially >= a past win_start), holding the battery full right
        # through the evening peak it was meant to discharge into.
        if win_end < ends[0]:
            day += timedelta(days=1)
            continue
        # Hold soc >= target across the window: every step whose END lands in
        # [win_start, win_end], plus the arrival instant (the first step-end at
        # or after win_start) so hold_hours=0 always pins one step.
        arrival = next((i for i, e in enumerate(ends) if e >= win_start), None)
        if arrival is None:
            day += timedelta(days=1)
            continue
        for i in range(arrival, len(ends)):
            if i == arrival or ends[i] <= win_end:
                target[i] = max(target[i], kwh)
            else:
                break
        day += timedelta(days=1)
    return target if np.any(target > 0) else None


def discharge_cap_vector(
    steps: int, live_spike: bool, spike_discharge_kw: float, max_discharge_kw: float
) -> np.ndarray | None:
    """Raised step-0 discharge cap during a CONFIRMED spike only."""
    if not live_spike or spike_discharge_kw <= max_discharge_kw:
        return None
    caps = np.full(steps, max_discharge_kw)
    caps[0] = spike_discharge_kw
    return caps


@dataclass
class CycleData:
    grid: TimeGrid
    inputs: OptimizerInputs
    prices: PriceForecast
    battery: BatteryState
    temps: np.ndarray | None
    # Where the real price forecast ends; steps beyond this hold the last
    # value (padding) and should be read with appropriate suspicion.
    price_forecast_end: datetime | None = None
    coverage: dict[str, float] | None = None
    # anything but "learned" means the plan assumes zero house load — surfaced
    # as a warning on the dashboard and numbat_status
    load_forecast_status: str = "learned"
    # how the model was learned (window, source, temp response) — dashboard
    load_forecast_info: dict = field(default_factory=dict)
    # {baseline_kw, until} while vacation mode is active, else None — drives
    # the dashboard banner and binary_sensor.numbat_vacation_mode
    vacation: dict | None = None


class Planner:
    def __init__(
        self,
        settings: Settings,
        *,
        prices: PriceProvider,
        solar: OpenMeteoSolarAdapter,
        battery: SungrowAdapter,
        weather: WeatherAdapter,
        tz: ZoneInfo,
        load_forecaster: LoadForecaster,
    ):
        self._settings = settings
        self._prices = prices
        self._solar = solar
        self._battery = battery
        self._weather = weather
        self._tz = tz
        self._load_forecaster = load_forecaster
        self._battery_params = battery_params(settings)
        self._grid_params = GridParams(
            import_limit_kw=settings.grid.import_limit_kw,
            export_limit_kw=settings.grid.export_limit_kw,
            min_battery_export_price=settings.grid.min_battery_export_price,
        )
        self.previous_plan: Plan | None = None

    async def gather(self, now: datetime) -> CycleData:
        # rate-limited internally; a no-op for the static profile forecaster
        await self._load_forecaster.refresh(now)
        prices, pv, battery = await asyncio.gather(
            self._prices.get_prices(),
            self._solar.get_pv(),
            self._battery.get_battery_state(),
        )
        temps_series: Series | None = None
        if self._settings.entities.weather:  # optional — unset means no temp response
            try:
                temps_series = await self._weather.get_temperature_forecast()
            except Exception as e:  # noqa: BLE001 - temps are optional, never fatal
                log.warning("temperature forecast unavailable (%s); load rules disabled", e)

        if prices.updated_at and now - prices.updated_at > MAX_PRICE_AGE:
            raise InputsStale(f"prices last updated {prices.updated_at.isoformat()}")
        if now - battery.ts > MAX_SOC_AGE:
            # Not fatal: the mkaiser package's battery sensors only report on
            # value CHANGE, so an idle battery at constant SoC looks "stale"
            # while being perfectly live. Unavailability is what the adapter
            # treats as fatal; age is just worth a note.
            log.info(
                "battery sensors last reported %s (only report on change; using as-is)",
                battery.ts.isoformat(),
            )

        horizon = timedelta(hours=self._settings.optimizer.horizon_hours)
        grid = TimeGrid.build(now, sorted({*prices.buy.times, *prices.sell.times}), horizon)

        buy = resample_previous(prices.buy, grid)
        sell_raw = resample_previous(prices.sell, grid)
        buy[0], sell_raw[0] = prices.current_buy, prices.current_sell
        # The haircut tempers the objective's trust in distant prices; the
        # spike reserve triggers on the RAW forecast — it exists precisely to
        # hedge prices the haircut would discount.
        sell = self._haircut_sell(sell_raw, grid, now)

        pv_kw = resample_mean(pv, grid)
        temps = resample_previous(temps_series, grid) if temps_series else None
        load_kw = self._load_forecaster.forecast(grid, temps)
        # Safety buffer: plan for consistently more than the learned mean.
        # After the temperature response (a buffered heatwave stays buffered),
        # before the feasibility clamp below.
        if (buffer := self._settings.load.buffer) > 0:
            load_kw = load_kw * (1.0 + buffer)
        # Vacation mode: overlay the flat standby baseline (unbuffered — it's
        # a deliberate number) over the steps the household is away; steps
        # after `until` keep the learned+buffered forecast, so a return date
        # inside the horizon already plans the real evening load.
        vacation = self._settings.vacation
        vacation_info: dict | None = None
        if vacation.active(now, self._tz):
            until_utc = vacation.until_utc(self._tz)
            away = np.array(
                [until_utc is None or s.start < until_utc for s in grid.steps]
            )
            load_kw = np.where(away, vacation.baseline_kw, load_kw)
            vacation_info = {
                "baseline_kw": vacation.baseline_kw,
                "until": vacation.until.isoformat() if vacation.until else None,
            }
        # Feasibility guard: the power balance can always serve load up to
        # import + PV (the battery may be empty, so its discharge doesn't
        # count); anything beyond that turns the MILP infeasible. Real load
        # above this bound is impossible at the meter anyway — a forecast
        # that exceeds it means bad sensor data, not bad planning.
        supply_cap = self._grid_params.import_limit_kw + pv_kw
        if np.any(load_kw > supply_cap):
            log.warning(
                "load forecast peaks at %.1f kW, beyond what import + PV can "
                "serve (%.1f kW); clamping — check the load sensor's units/data",
                float(np.max(load_kw)),
                float(np.max(supply_cap)),
            )
            load_kw = np.minimum(load_kw, supply_cap)

        inputs = OptimizerInputs(
            dt_hours=grid.dt_hours,
            buy=buy,
            sell=sell,
            pv=pv_kw,
            load=load_kw,
            soc0_kwh=battery.soc_frac * self._battery_params.capacity_kwh,
            reserve_kwh=self._spike_reserve(sell_raw, grid, now, prices),
            max_discharge_kw_step=self._discharge_caps(len(grid), prices.live_spike),
            soc_target_kwh=daily_soc_target_vector(
                grid,
                self._tz,
                target_soc=self._settings.battery.daily_target_soc,
                target_time=self._settings.battery.daily_target_time,
                hold_hours=self._settings.battery.daily_target_hold_hours,
                capacity_kwh=self._battery_params.capacity_kwh,
                soc_max_kwh=self._battery_params.soc_max_kwh,
            ),
        )
        cov = {
            "buy": round(coverage(prices.buy, grid), 3),
            "sell": round(coverage(prices.sell, grid), 3),
            "pv": round(coverage(pv, grid), 3),
        }
        if min(cov.values()) < 0.7:
            log.warning(
                "forecast coverage low (%s): steps beyond the forecast hold the "
                "last value — tail of the plan is speculative",
                cov,
            )
        return CycleData(
            grid=grid,
            inputs=inputs,
            prices=prices,
            battery=battery,
            temps=temps,
            price_forecast_end=min(prices.buy.end, prices.sell.end),
            coverage=cov,
            load_forecast_status=self._load_forecaster.status,
            load_forecast_info=(
                {**self._load_forecaster.details, "buffer": buffer}
                if buffer > 0
                else self._load_forecaster.details
            ),
            vacation=vacation_info,
        )

    def _discharge_caps(self, steps: int, live_spike: bool) -> np.ndarray | None:
        caps = discharge_cap_vector(
            steps,
            live_spike,
            self._settings.spike.discharge_kw,
            self._battery_params.max_discharge_kw,
        )
        if caps is not None:
            log.info("confirmed spike: step-0 discharge cap raised to %.1f kW", caps[0])
        return caps

    def _haircut_sell(self, sell: np.ndarray, grid: TimeGrid, now: datetime) -> np.ndarray:
        """Discount above-median sell prices beyond HAIRCUT_START toward the
        median: distant forecast spikes shouldn't distort near-term decisions."""
        h = self._settings.optimizer.forecast_haircut
        if h <= 0:
            return sell
        median = float(np.median(sell))
        out = sell.copy()
        for i, step in enumerate(grid.steps):
            if step.start - now >= HAIRCUT_START and out[i] > median:
                out[i] = median + (out[i] - median) * (1 - h)
        return out

    def _spike_reserve(
        self, sell: np.ndarray, grid: TimeGrid, now: datetime, prices: PriceForecast
    ) -> np.ndarray | None:
        cfg = self._settings.spike
        reserve = spike_reserve_vector(
            sell,
            grid.dt_hours,
            lookahead_hours=cfg.lookahead_hours,
            high_price_threshold=cfg.high_price_threshold,
            reserve_kwh=cfg.reserve_kwh,
            soc_max_kwh=self._battery_params.soc_max_kwh,
        )
        if reserve is not None:
            trigger = int(np.argmin(reserve > 0))
            log.info(
                "spike reserve armed: %.1f kWh held until %s (sell %.2f $/kWh)",
                reserve[0],
                grid.steps[trigger].start.isoformat(),
                sell[trigger],
            )
        return reserve

    def optimize(self, data: CycleData, now: datetime) -> Plan:
        cfg = self._settings.optimizer
        # Anchor the hold value on the REAL forecast window only — the padded
        # tail repeats the last value and would drag min()/median() around.
        real_buy = self._real_forecast_buy(data)
        terminal = (
            auto_terminal_value(
                real_buy,
                self._battery_params,
                floor=cfg.hold_value_floor,
                scaling=cfg.hold_value_scaling,
            )
            if cfg.terminal_soc_value == "auto"
            else float(cfg.terminal_soc_value)
        )
        opt_config = OptimizerConfig(
            terminal_value=terminal,
            reserve_penalty_per_kwh=self._settings.spike.reserve_penalty_per_kwh,
            solver_timeout_s=cfg.solver_timeout_s,
            soc_target_penalty_per_kwh=self._daily_target_penalty(real_buy),
            min_battery_export_spread=cfg.min_battery_export_spread,
            import_penalty_per_kwh=cfg.import_penalty_per_kwh,
        )
        solution = solve(data.inputs, self._battery_params, self._grid_params, opt_config)
        solution = self._apply_hysteresis(solution, data, opt_config)
        plan = solution_to_plan(solution, data.grid, data.inputs, computed_at=now)
        if solution.status.endswith("(hysteresis)"):
            plan.solver_status = solution.status
        plan.live_spike = data.prices.live_spike
        plan = self._live_spike_guard(plan, data)
        plan.explanation = build_explanation(
            plan,
            hold_value=terminal,
            price_forecast_end=data.price_forecast_end,
            spike_reserve=self._reserve_info(data),
            daily_target_active=(
                data.inputs.soc_target_kwh is not None
                and bool(np.any(data.inputs.soc_target_kwh > 0))
            ),
            live_spike=data.prices.live_spike,
            prices_estimated=data.prices.current_estimate,
            capacity_kwh=self._battery_params.capacity_kwh,
            tz=self._tz,
        )
        return plan

    def _real_forecast_buy(self, data: CycleData) -> np.ndarray:
        """The buy prices from the genuine forecast window, dropping the padded
        tail (steps at/after price_forecast_end repeat the last value)."""
        end = data.price_forecast_end
        if end is None:
            return data.inputs.buy
        real = np.array([s.start < end for s in data.grid.steps])
        return data.inputs.buy[real] if real.any() else data.inputs.buy

    def _daily_target_penalty(self, real_buy: np.ndarray) -> float:
        """The daily-target premium ($/kWh-hour of shortfall). Optionally lifted
        to dominate the tariff — a multiple of the median forward import — so a
        set target actually gets filled instead of losing to evening prices."""
        b = self._settings.battery
        penalty = b.daily_target_penalty_per_kwh
        if b.daily_target_penalty_price_multiple > 0 and real_buy.size:
            scaled = b.daily_target_penalty_price_multiple * float(np.median(real_buy))
            penalty = max(penalty, scaled)
        return penalty

    def _reserve_info(self, data: CycleData) -> dict | None:
        """The spike reserve as {kwh, until} for the explanation, or None when
        it isn't armed this cycle."""
        reserve = data.inputs.reserve_kwh
        if reserve is None or not np.any(reserve > 0):
            return None
        trigger = int(np.argmin(reserve > 0))  # first step the floor drops to 0
        until = data.grid.steps[trigger].start if trigger < len(data.grid.steps) else None
        return {"kwh": float(reserve[0]), "until": until.isoformat() if until else None}

    def _apply_hysteresis(self, free, data: CycleData, opt_config: OptimizerConfig):
        """Only switch away from the previous action if the free solution beats
        the action-pinned solution by more than the configured threshold —
        compared on the FULL solver objective (energy + wear + terminal value),
        not just the energy bill."""
        threshold = self._settings.optimizer.action_switch_threshold_dollars
        prev = self.previous_plan
        if prev is None or not prev.intervals or threshold <= 0:
            return free
        prev_action = prev.intervals[0].action
        free_action = classify_action(
            float(free.charge_kw[0]),
            float(free.discharge_kw[0]),
            float(data.inputs.pv[0]),
            float(free.pv_used_kw[0]),
            float(data.inputs.load[0]),
        )
        if free_action == prev_action:
            return free
        try:
            pinned = solve(
                data.inputs,
                self._battery_params,
                self._grid_params,
                opt_config,
                pin_step0=prev_action.value,
            )
        except SolverError:
            return free  # previous action no longer feasible; switch
        gain = pinned.objective - free.objective
        if gain < threshold:
            log.debug("hysteresis: keeping %s (switch would gain only $%.4f)", prev_action, gain)
            pinned.status = f"{pinned.status} (hysteresis)"
            return pinned
        return free

    def _live_spike_guard(self, plan: Plan, data: CycleData) -> Plan:
        """Belt-and-braces: never grid-charge during a confirmed price spike."""
        if not data.prices.live_spike or not plan.intervals:
            return plan
        step0 = plan.intervals[0]
        if step0.action == Action.CHARGE and step0.grid_import_kw > 0.01:
            log.warning("live spike active: suppressing planned grid charge")
            step0.action = Action.IDLE
            step0.power_kw = 0.0
        return plan

    async def run_cycle(self, now: datetime | None = None) -> Plan:
        now = now or datetime.now(UTC)
        try:
            data = await self.gather(now)
            plan = self.optimize(data, now)
        except SolverError as e:
            log.error("solver failed: %s", e)
            plan = self.fallback(now)
        self.previous_plan = plan
        return plan

    def fallback(self, now: datetime) -> Plan:
        """Shift the previous plan forward, dropping elapsed intervals."""
        prev = self.previous_plan
        if prev is None:
            raise SolverError("solver failed and no previous plan to fall back on")
        remaining = [iv for iv in prev.intervals if iv.end > now]
        if not remaining:
            raise SolverError("solver failed and previous plan is fully elapsed")
        s0 = remaining[0]
        return Plan(
            intervals=remaining,
            objective_cost=prev.objective_cost,
            solver_status="stale (reusing previous plan)",
            solve_ms=0.0,
            computed_at=prev.computed_at,
            # carry the spike flag so the published live_spike attribute stays
            # truthful while a fallback plan is in effect
            live_spike=prev.live_spike,
            # The full context is gone with the failed solve; give the panel the
            # step-0 values and an honest note rather than a stale reason.
            explanation={
                "reason": (
                    "Reusing the previous plan — the latest solve failed, so Numbat shifted "
                    "the last good plan forward. Values are from that plan."
                ),
                "values": {
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
                },
                "stale": True,
            },
        )


def battery_params(settings: Settings) -> BatteryParams:
    b = settings.battery
    return BatteryParams(
        capacity_kwh=b.capacity_kwh,
        max_charge_kw=b.max_charge_kw,
        max_discharge_kw=b.max_discharge_kw,
        efficiency_charge=b.efficiency_charge,
        efficiency_discharge=b.efficiency_discharge,
        soc_min_kwh=b.soc_min * b.capacity_kwh,
        soc_max_kwh=b.soc_max * b.capacity_kwh,
        wear_cost_per_kwh=b.wear_cost_per_kwh,
        allow_grid_charge=b.allow_grid_charge,
    )
