"""One optimization cycle: gather -> normalize -> solve -> Plan.

Also owns the judgment calls around the raw MILP:
- staleness policy (degraded inputs must never silently produce a plan)
- step-0 price override with the live 5-min prices
- forecast haircut (forecast sell prices discounted toward the median — only
  the live, confirmed price is trusted in full)
- spike reserve (a sales floor released by spike-level prices; execution is
  gated on the live confirmed price — only step 0 ever acts)
- hysteresis (pin-and-compare before switching the current action)
- live-spike guard (never grid-charge during a confirmed spike)
- fallback (reuse the previous plan when the solver fails)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
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
from numbat.flow import PV_OFF_ENTRY_BUY
from numbat.flow import annotate as annotate_flows
from numbat.flow import details as flow_details
from numbat.forecast.load import LoadForecaster
from numbat.models import Action, BatteryState, Plan, PlanInterval, PriceForecast, Series
from numbat.optimizer.model import (
    BatteryParams,
    GridParams,
    OptimizerConfig,
    OptimizerInputs,
    SolverError,
    auto_terminal_value,
    solve,
)
from numbat.optimizer.result import (
    POWER_TOL_KW,
    classify_action,
    hold_floor_kwh,
    solution_to_plan,
)
from numbat.timegrid import TimeGrid, coverage, resample_mean, resample_previous

log = logging.getLogger(__name__)

MAX_PRICE_AGE = timedelta(minutes=15)
MAX_SOC_AGE = timedelta(minutes=10)


class InputsStale(Exception):
    pass


def haircut_sell(sell: np.ndarray, haircut: float) -> np.ndarray:
    """Shave `haircut` (a fraction) off every FORECAST sell price's excess
    above the median. Forecasts run optimistic even one interval out — around
    spikes especially — so only step 0 (the live, confirmed price) is trusted
    in full. Flat by design: one rule, easy to reason about. Below-median
    prices are untouched. (The spike reserve reads this trimmed series for
    its in-plan releases — a marginal forecast spike the trim drops below
    the release price isn't anticipated — while step 0's confirmed price is
    exempt from the trim, so real releases are never haircut away.) Shared
    by the live planner and test mode (scenarios + time travel), so the
    sandbox knob behaves exactly like the live one."""
    if haircut <= 0 or len(sell) < 2:
        return sell
    median = float(np.median(sell))
    out = sell.copy()
    tail = out[1:]
    out[1:] = np.where(tail > median, median + (tail - median) * (1 - haircut), tail)
    return out


def sell_floor_vector(
    sell: np.ndarray,
    *,
    reserve_kwh: float,
    export_reserve_kwh: float,
    high_price_threshold: float,
) -> np.ndarray | None:
    """The spike reserve as a per-step SALES floor (kWh, aligned with
    soc[1:]): ordinary sales stop at the reserve; a step sells through it
    only at a price above the release threshold. Pass the SOLVE series, so
    step 0 releases on the live CONFIRMED price — the execution gate, since
    only step 0 ever acts — while future steps release on the (haircut)
    forecast, letting the plan anticipate the sale, pre-position for it,
    and show it honestly on the dashboard and in simulations. A phantom
    forecast spike can never actually spend the reserve: when its interval
    arrives un-confirmed, that cycle's step 0 stays floored and the foreseen
    sale evaporates in the re-solve. During a real spike the 5-minute/event
    re-solves roll the confirmed release forward while it lasts. None =
    disabled, or inert (not above the export reserve)."""
    if reserve_kwh <= export_reserve_kwh:
        return None
    return np.where(sell > high_price_threshold, export_reserve_kwh, reserve_kwh)


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
        # clamp: a target above soc_max would bake an unavoidable phantom
        # penalty into every objective
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
    sell: np.ndarray,
    live_spike: bool,
    spike_discharge_kw: float,
    max_discharge_kw: float,
    high_price_threshold: float,
) -> np.ndarray | None:
    """Per-step discharge caps: the everyday wear-conscious limit, raised to
    the spike cap at step 0 while a spike is live (the current sell price
    clears the release threshold — see Planner._live_spike),
    and at future steps whose forecast clears the release threshold — if
    those spikes confirm, the live cap will be raised when their interval
    arrives, so the plan should model the power it will actually have
    (matters for scheduling anticipated spike-reserve sales). None when the
    spike cap is disabled or nothing raises it."""
    if spike_discharge_kw <= max_discharge_kw:
        return None
    caps = np.full(len(sell), max_discharge_kw)
    caps[1:] = np.where(sell[1:] > high_price_threshold, spike_discharge_kw, max_discharge_kw)
    if live_spike:
        caps[0] = spike_discharge_kw
    return caps if caps.max() > max_discharge_kw else None


@dataclass
class CycleData:
    grid: TimeGrid
    inputs: OptimizerInputs
    prices: PriceForecast
    battery: BatteryState
    temps: np.ndarray | None
    # inputs.sell with the forecast haircut undone — what the plan/dashboard
    # reports, so displayed prices and revenue match the real forecast
    sell_raw: np.ndarray | None = None
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


# PV_OFF_ENTRY_BUY (numbat.flow): enter PV-off only once the live buy price
# is at least a cent negative. Each clear costs ~40–50 s of generation (the
# MPPT restart), so a price hovering around zero on a 5-minute site must not
# toggle the actuator every interval; a cent below zero is roughly where an
# hour of paid import for a 1 kW house covers one restart. Once on, it stays
# on while the buy price is negative at all (asymmetric, like a thermostat).


def pv_off_wanted(
    current_buy: float, step0: PlanInterval, *, previously: bool, estimate: bool = False
) -> bool:
    """PV generation should be stopped this interval: the plan uses none of
    the PV the step has (at a negative buy the optimizer serves the house —
    and any charge — from the grid, which pays; the flag then rides hold or
    charge, or idle/curtail in the edge cases of a battery at its floor or a
    dawn trickle — all safe with PV off, since the plan uses none), and the
    live buy price is negative: below PV_OFF_ENTRY_BUY to switch on, below
    zero to stay on. While the live price is still an Amber estimate the
    previous state is kept — the confirmed price re-solves within seconds,
    and an estimate must not flip a slow actuator twice in one interval."""
    if not (step0.pv_kw > POWER_TOL_KW and step0.pv_used_kw < POWER_TOL_KW):
        return False
    if estimate:
        return previously
    return current_buy < (0.0 if previously else PV_OFF_ENTRY_BUY)


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
        # The haircut tempers the objective's trust in forecast prices (the
        # live step-0 price is exempt). The published plan still shows raw
        # prices: the haircut is planning maths, not a dollar the meter will
        # see. The spike reserve's in-plan releases read the trimmed series;
        # its execution gate (step 0) is the confirmed price the haircut
        # never touches.
        sell = self._haircut_sell(sell_raw)

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
            sell_floor_kwh=self._sell_floor(sell),
            max_discharge_kw_step=self._discharge_caps(sell, self._live_spike(prices)),
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
            sell_raw=sell_raw,
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

    def _live_spike(self, prices: PriceForecast) -> bool:
        """A spike is live when the current feed-in price clears
        spike.high_price_threshold — the same gate that releases the spike
        reserve, so every spike behavior keys off one user-set number.
        In an interval's first seconds that price can still be Amber's
        estimate (same nuance as the reserve release, documented in DOCS);
        the estimate->confirmed re-solve corrects within seconds."""
        return prices.current_sell > self._settings.spike.high_price_threshold

    def _discharge_caps(self, sell: np.ndarray, live_spike: bool) -> np.ndarray | None:
        caps = discharge_cap_vector(
            sell,
            live_spike,
            self._settings.spike.discharge_kw,
            self._battery_params.max_discharge_kw,
            self._settings.spike.high_price_threshold,
        )
        if caps is not None and live_spike:
            log.info("confirmed spike: step-0 discharge cap raised to %.1f kW", caps[0])
        return caps

    def _haircut_sell(self, sell: np.ndarray) -> np.ndarray:
        return haircut_sell(sell, self._settings.optimizer.forecast_haircut)

    def _sell_floor(self, sell: np.ndarray) -> np.ndarray | None:
        cfg = self._settings.spike
        floor = sell_floor_vector(
            sell,
            reserve_kwh=cfg.reserve_soc * self._battery_params.capacity_kwh,
            export_reserve_kwh=self._battery_params.export_reserve_kwh,
            high_price_threshold=cfg.high_price_threshold,
        )
        if floor is not None and sell[0] > cfg.high_price_threshold:
            log.info(
                "spike reserve released: confirmed %.2f $/kWh clears the %.2f threshold",
                float(sell[0]),
                cfg.high_price_threshold,
            )
        return floor

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
            solver_timeout_s=cfg.solver_timeout_s,
            soc_target_penalty_per_kwh=self._daily_target_penalty(real_buy),
            min_battery_export_spread=cfg.min_battery_export_spread,
            import_penalty_per_kwh=cfg.import_penalty_per_kwh,
        )
        solution = solve(data.inputs, self._battery_params, self._grid_params, opt_config)
        solution = self._apply_hysteresis(solution, data, opt_config)
        # Report raw prices: the haircut shapes the solve, but displayed
        # prices/revenue must match the real forecast (same philosophy as the
        # import-reluctance toll — planning maths only).
        display_inputs = (
            replace(data.inputs, sell=data.sell_raw) if data.sell_raw is not None else data.inputs
        )
        plan = solution_to_plan(
            solution,
            data.grid,
            display_inputs,
            computed_at=now,
            # HOLD only while the battery has something worth holding
            hold_floor_kwh=hold_floor_kwh(
                self._battery_params.soc_min_kwh, self._battery_params.capacity_kwh
            ),
        )
        if solution.status.endswith("(hysteresis)"):
            plan.solver_status = solution.status
        plan.live_spike = self._live_spike(data.prices)
        # Withhold export while feed-in is negative and the plan exports
        # nothing: the actuator caps the export limit even mid-charge (the
        # action alone can't say "charge AND don't export the PV surplus" —
        # 2026-09-10: entering the charge branch lifted the cap and the
        # surplus exported at −12c). Gated on a negative live price so a
        # PV surprise at positive feed-in still exports freely.
        plan.curtail_export = (
            data.prices.current_sell < 0 and plan.intervals[0].grid_export_kw < 0.05
        )
        plan = self._live_spike_guard(plan, data)
        # Stop PV while the buy price is negative and the plan uses none of
        # it: the optimizer's PV usage is free (0 ≤ pv_used ≤ pv), so at a
        # negative buy it already serves the house — and any charge — from
        # the grid, which pays; the hardware just couldn't follow until an
        # actuator could switch PV off (Sungrow SH-T, 2026-09-11). Gated on
        # the LIVE buy price like curtail on feed-in (pv_used = 0 also shows
        # up in cost ties around buy ≈ 0) with an entry deadband and a hold
        # through estimates — see pv_off_wanted. Not while the plan still
        # uses some PV (e.g. an import limit the house alone would exceed).
        # After the spike guard, so the flag describes the action it rides.
        plan.pv_off = pv_off_wanted(
            data.prices.current_buy,
            plan.intervals[0],
            previously=self.previous_plan.pv_off if self.previous_plan else False,
            estimate=data.prices.current_estimate,
        )
        annotate_flows(plan)
        plan.explanation = build_explanation(
            plan,
            hold_value=terminal,
            spike_reserve=self._reserve_info(data),
            daily_target_active=(
                data.inputs.soc_target_kwh is not None
                and bool(np.any(data.inputs.soc_target_kwh > 0))
            ),
            live_spike=self._live_spike(data.prices),
            prices_estimated=data.prices.current_estimate,
            capacity_kwh=self._battery_params.capacity_kwh,
            curtail=plan.curtail_export,
            pv_off=plan.pv_off,
            soc_max_kwh=self._battery_params.soc_max_kwh,
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
        """The spike reserve for the explanation chip: its size, the release
        threshold, and whether the live price has released it. None when
        disabled or inert (not above the export reserve)."""
        cfg = self._settings.spike
        if cfg.reserve_soc * self._battery_params.capacity_kwh <= (
            self._battery_params.export_reserve_kwh
        ):
            return None
        return {
            "soc": cfg.reserve_soc,
            "threshold": cfg.high_price_threshold,
            "released": data.prices.current_sell > cfg.high_price_threshold,
        }

    def _apply_hysteresis(self, free, data: CycleData, opt_config: OptimizerConfig):
        """Only switch away from the previous action if the free solution beats
        the action-pinned solution by more than the configured threshold —
        compared on the FULL solver objective (energy + wear + terminal value),
        not just the energy bill.

        Known one-way seam: some pins define an ENVELOPE that contains other
        actions' flows — pin "idle" (self-consumption) admits the hold shape
        (pc=pd=0, grid serves load), and pin "hold" admits no_charge's at a
        PV ramp — so a kept pinned solution can still RELABEL (idle->hold,
        hold->no_charge) without paying the threshold. That is accepted:
        those transitions swap only battery-limit registers (cheap, guarded),
        the exits that matter (anything -> charge/discharge) are always
        thresholded, and the reverse relabels can't oscillate because the
        tighter pin does hold its own label."""
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
            holdable=float(free.soc_kwh[0])
            > hold_floor_kwh(
                self._battery_params.soc_min_kwh, self._battery_params.capacity_kwh
            ),
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
        if not self._live_spike(data.prices) or not plan.intervals:
            return plan
        step0 = plan.intervals[0]
        if step0.action == Action.CHARGE and step0.grid_import_kw > 0.01:
            log.warning("live spike active: suppressing planned grid charge")
            step0.action = Action.IDLE
            # Re-state step 0's numbers as what `idle` actuates during a
            # spike: self-consumption, the battery covering the house's
            # shortfall. The rest of the plan still assumes the charge
            # happened (it re-solves within minutes); only this interval —
            # the one published and drawn as "now" — is made consistent.
            bp = self._battery_params
            dt = (step0.end - step0.start).total_seconds() / 3600
            shortfall = max(0.0, step0.load_kw - step0.pv_used_kw)
            spendable = max(0.0, step0.soc_start - bp.soc_min_kwh) * bp.efficiency_discharge / dt
            discharge = min(shortfall, bp.max_discharge_kw, spendable)
            step0.power_kw = -discharge
            step0.grid_import_kw = max(0.0, shortfall - discharge)
            step0.soc_end = step0.soc_start - discharge * dt / bp.efficiency_discharge
            step0.interval_cost = (
                step0.buy * step0.grid_import_kw - step0.sell * step0.grid_export_kw
            ) * dt
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
        plan = Plan(
            intervals=remaining,
            objective_cost=prev.objective_cost,
            solver_status="stale (reusing previous plan)",
            solve_ms=0.0,
            computed_at=prev.computed_at,
            # carry the spike and curtail flags so the published attributes
            # stay truthful while a fallback plan is in effect. Curtail is
            # additionally gated on the surviving step's own export intent:
            # the live price is unknowable here, and a carried cap must not
            # throttle a step that planned to sell (e.g. an evening export
            # after a negative midday, solver failing at the rollover).
            live_spike=prev.live_spike,
            curtail_export=prev.curtail_export and s0.grid_export_kw < 0.05,
            # same shape for PV, against the surviving step's own forecast
            # buy: a carried PV-off is kept only while that step planned to
            # use no PV AND its price is still negative — a solver outage
            # across the rollover to a paid interval must not keep PV off
            pv_off=prev.pv_off and pv_off_wanted(s0.buy, s0, previously=True),
            # The full context is gone with the failed solve; give the panel the
            # step-0 values — the "reusing previous plan" chip (stale) says why.
            explanation={
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
        # the flags as published (carried above), so the dashboard's
        # reconciliation line stays truthful on a stale plan
        plan.explanation["levers"] = {
            "live_spike": plan.live_spike,
            "curtail": plan.curtail_export,
            "pv_off": plan.pv_off,
        }
        annotate_flows(plan)
        plan.explanation["flow"] = flow_details(plan, self._battery_params.capacity_kwh)
        return plan


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
        export_reserve_kwh=b.export_reserve_soc * b.capacity_kwh,
    )
