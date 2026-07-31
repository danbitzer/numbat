"""Household load forecasting — learned from actual consumption, or nothing.

There is deliberately no hand-typed hourly profile: HistoryLoadForecaster
learns hour-of-day × weekday/weekend averages from the household's real
consumption (entities.load_power), refreshed daily. Preferred data source is
hourly long-term statistics (which survive recorder purging, so the window
can be months), falling back to raw recorder history (~10 days) for sensors
without a state_class. The fit is recency-weighted (RECENCY_HALF_LIFE_DAYS)
so the current season's habits dominate the year of history instead of being
averaged away.

With an outdoor temperature sensor configured (entities.outdoor_temp), the
daily learn also fits a temperature response: pooled cooling/heating slopes
(kW per degree above/below balance temperatures) regressed against each hour
bucket's deviation from its own average. forecast() then applies the FORECAST
temperatures to those slopes — so the prediction tracks a heatwave arriving
after a mild fortnight instead of the trailing average lagging it. The model
is only ever fitted on hours where load AND temperature overlap, so a young
temperature sensor caps the effective window rather than skewing the fit.

Until learning succeeds (no load sensor configured, or no usable history yet)
Numbat plans with ZERO house load and reports the degraded state via `status`,
which the dashboard and numbat_status surface as a warning. The documented
mitigation is raising battery.soc_min until learning is active.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np

from numbat.ha.client import HaClient
from numbat.timegrid import TimeGrid

log = logging.getLogger(__name__)

# A raw-history sample holds until the next one; cap how long a single sample
# can count for so recorder gaps (HA restarts, purged data) don't let one
# stale value dominate its bucket.
MAX_SEGMENT = timedelta(minutes=30)
# Minimum observed hours in a (daytype, hour) bucket before trusting it over
# the mean of the buckets that do have data.
MIN_BUCKET_HOURS = 2.0
# Minimum hours carrying both load and temperature before fitting slopes —
# 3 days of joint data; below that the regression is noise.
MIN_TEMP_HOURS = 72
# Minimum hourly statistics rows to prefer LTS over raw recorder history.
MIN_STATS_HOURS = 24
# How far back each learn reads. Not configurable: more history is strictly
# better (the window self-caps to what the sensors actually have), and a year
# bounds the query while covering every season.
HISTORY_DAYS = 365
# Recency half-life for the fit: a sample this many days old counts half as
# much as one from today. A straight mean over the window smears seasons —
# a winter weekend's ~3 kW morning heating block averaged against summer's
# ~0.3 kW mornings under-forecasts cold mornings by kilowatts (seen live
# 2026-08-01: the battery was run flat by a 4.4 kW morning block the plan
# never saw coming). Three weeks keeps the current season dominant (~half
# the total weight comes from the last three weeks) while months of history
# still steady the thin weekend buckets. Not configurable, same reasoning
# as HISTORY_DAYS.
RECENCY_HALF_LIFE_DAYS = 21.0
REFRESH_INTERVAL = timedelta(hours=24)
RETRY_INTERVAL = timedelta(minutes=30)
# Unit plausibility: no house has a median hourly load above ~50 kW or below
# ~10 W, so a daily median outside these bounds means the declared unit lies
# (e.g. the mkaiser package's load_power declares kW while emitting
# watt-magnitude values — seen live on Dan's install, where it produced a
# 35 kW/°C heating slope and an infeasible MILP). Checked per UTC day, not
# per window, so a window that MIXES magnitudes (sensor fixed mid-window)
# gets each side corrected instead of one side poisoned.
MAX_PLAUSIBLE_MEDIAN_KW = 50.0
MIN_PLAUSIBLE_MEDIAN_KW = 0.01
# One learn must never eat the 90s cycle budget (WS handshakes against a
# blocked recorder can take ~45s each); on timeout the model waits for the
# retry interval like any other failure.
LEARN_TIMEOUT_S = 45
# Physics plausibility for the fitted temperature response: whole-house
# heating/cooling beyond this per degree means the fit chased an artifact —
# drop the response rather than forecast nonsense.
MAX_SLOPE_KW_PER_DEG = 2.0
# The temperature response is schedule-gated (heating runs at breakfast and
# in the evening, not at 3am), so one pooled slope dilutes it badly — seen on
# Dan's data: 0.12 kW/°C at 5-6am vs 0.035 pooled. Slopes are refined per
# hour block, falling back to the pooled slope where a block is thin.
HOUR_BLOCKS = (
    (22, 23, 0, 1, 2, 3, 4),  # night
    tuple(range(5, 10)),  # morning
    tuple(range(10, 16)),  # day
    tuple(range(16, 22)),  # evening
)
MIN_BLOCK_TEMP_HOURS = 48

_UNIT_TO_KW = {"W": 0.001, "kW": 1.0, "w": 0.001, "kw": 1.0}

# learned: a model is active. pending: a load sensor is configured but no
# model has been learned yet. unconfigured: no load sensor at all. Anything
# but "learned" means Numbat is planning with zero house load.
LoadForecastStatus = Literal["learned", "pending", "unconfigured"]


class LoadForecaster(Protocol):
    @property
    def status(self) -> LoadForecastStatus: ...

    @property
    def details(self) -> dict[str, Any]:
        """How the current model was learned (for the dashboard); {} if none."""
        ...

    async def refresh(self, now: datetime) -> None:
        """Update any learned state (rate-limited internally; never raises)."""
        ...

    def forecast(self, grid: TimeGrid, temps_c: np.ndarray | None) -> np.ndarray:
        """Expected household load in kW per grid step."""
        ...


class UnconfiguredLoadForecaster:
    """No load sensor: plan with zero load and say so loudly."""

    status: LoadForecastStatus = "unconfigured"
    details: dict[str, Any] = {}

    async def refresh(self, now: datetime) -> None:
        return None

    def forecast(self, grid: TimeGrid, temps_c: np.ndarray | None) -> np.ndarray:
        return np.zeros(len(grid))


def _step_bucket(step_start: datetime, step_end: datetime, tz: ZoneInfo) -> tuple[int, int]:
    """(is_weekend, local hour) for a grid step, keyed by its midpoint."""
    mid = (step_start + (step_end - step_start) / 2).astimezone(tz)
    return (1 if mid.weekday() >= 5 else 0, mid.hour)


def _local_hour_pieces(
    start: datetime, end: datetime, tz: ZoneInfo
) -> list[tuple[int, int, float]]:
    """Split [start, end) at local hour boundaries: (is_weekend, hour, hours).

    Statistics rows start on UTC hour boundaries, which in a half-hour-offset
    zone like Adelaide (+09:30) is local hh:30 — bucketing such a row whole
    (by midpoint) shifts the entire learned daily profile ~30 min late.
    Splitting weights each covered local hour by actual overlap instead.
    """
    pieces = []
    cur = start
    while cur < end:
        local = cur.astimezone(tz)
        boundary = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        nxt = min(boundary.astimezone(cur.tzinfo), end)
        pieces.append(
            (1 if local.weekday() >= 5 else 0, local.hour, (nxt - cur).total_seconds() / 3600)
        )
        cur = nxt
    return pieces


@dataclass
class LoadModel:
    """Learned hourly baseline plus optional pooled temperature response.

    Prediction: base[bucket] + cool_slope * (cdh(T) - bucket's mean cdh)
                             + heat_slope * (hdh(T) - bucket's mean hdh)
    i.e. unbiased at the bucket's historically-typical temperature, shifted by
    how far the forecast deviates from it.
    """

    base: list[list[float | None]]  # (daytype, hour) mean kW; None = thin bucket
    cdh_mean: np.ndarray  # (2, 24) mean cooling degrees per bucket
    hdh_mean: np.ndarray
    cool_kw_per_deg: float = 0.0  # pooled across all hours
    heat_kw_per_deg: float = 0.0
    # per-local-hour refinements (fitted per HOUR_BLOCKS); None = use pooled
    cool_by_hour: list[float] | None = None
    heat_by_hour: list[float] | None = None
    balance_cool_c: float = 22.0
    balance_heat_c: float = 15.0
    has_temp_response: bool = False
    # highest hourly load ever observed — predictions never extrapolate past
    # it, no matter what the forecast temperature says
    max_kw: float = float("inf")
    # learn-window facts, surfaced on the dashboard
    window_days: float = 0.0  # span of the data actually fitted
    hours_used: int = 0  # hourly rows in that window (0 for raw history)
    half_life_days: float = 0.0  # recency half-life used (0 = unweighted)
    temp_hours: int = 0  # hourly rows the temperature regression saw

    @property
    def known_buckets(self) -> int:
        return sum(v is not None for table in self.base for v in table)

    @property
    def mean_kw(self) -> float:
        """Mean of the known buckets — the fallback for thin ones."""
        values = [v for table in self.base for v in table if v is not None]
        return float(np.mean(values)) if values else 0.0

    def predict(self, weekend: int, hour: int, temp_c: float | None) -> float:
        value = self.base[weekend][hour]
        if value is None:
            return self.mean_kw
        if self.has_temp_response and temp_c is not None:
            cool = self.cool_by_hour[hour] if self.cool_by_hour else self.cool_kw_per_deg
            heat = self.heat_by_hour[hour] if self.heat_by_hour else self.heat_kw_per_deg
            cdh = max(temp_c - self.balance_cool_c, 0.0)
            hdh = max(self.balance_heat_c - temp_c, 0.0)
            value += cool * (cdh - self.cdh_mean[weekend][hour])
            value += heat * (hdh - self.hdh_mean[weekend][hour])
        return min(max(value, 0.0), self.max_kw)


def learn_hourly_profile(
    samples: list[tuple[datetime, float]],
    tz: ZoneInfo,
    *,
    min_bucket_hours: float = MIN_BUCKET_HOURS,
    max_segment: timedelta = MAX_SEGMENT,
) -> list[list[float | None]]:
    """Time-weighted mean kW per (daytype, local hour) from a piecewise-constant
    sample series. Returns [weekday, weekend] 24-value tables; None where the
    bucket has less than min_bucket_hours of observed time.

    Pure so it's testable without HA; samples must be time-ascending kW values.
    """
    energy = np.zeros((2, 24))  # kWh accumulated per bucket
    hours = np.zeros((2, 24))
    for (t0, v0), (t1, _) in zip(samples, samples[1:], strict=False):
        dur = min(t1 - t0, max_segment)
        if dur.total_seconds() <= 0:
            continue
        weekend, hour = _step_bucket(t0, t0 + dur, tz)
        dt_h = dur.total_seconds() / 3600
        energy[weekend][hour] += max(v0, 0.0) * dt_h
        hours[weekend][hour] += dt_h
    return [
        [
            float(energy[d][h] / hours[d][h]) if hours[d][h] >= min_bucket_hours else None
            for h in range(24)
        ]
        for d in (0, 1)
    ]


def _fit_slopes(dc: np.ndarray, dh: np.ndarray, dy: np.ndarray) -> tuple[float, float]:
    """Non-negative least squares for two regressors, by hand (it's 2x2).

    Degenerate columns (no temperature variation in that direction over the
    window — e.g. no cooling degrees in winter) get a zero slope.
    """

    def single(x: np.ndarray) -> float:
        d = float(x @ x)
        return max(float(x @ dy) / d, 0.0) if d > 1e-9 else 0.0

    has_c, has_h = float(dc @ dc) > 1e-9, float(dh @ dh) > 1e-9
    if has_c and has_h:
        a = np.array([[dc @ dc, dc @ dh], [dc @ dh, dh @ dh]])
        b = np.array([dc @ dy, dh @ dy])
        if abs(np.linalg.det(a)) > 1e-9:
            c, h = (float(v) for v in np.linalg.solve(a, b))
            if c < 0:
                return 0.0, single(dh)
            if h < 0:
                return single(dc), 0.0
            return c, h
        return single(dc), 0.0  # collinear: attribute to cooling arbitrarily
    return (single(dc), 0.0) if has_c else (0.0, single(dh))


def fit_load_model(
    records: list[tuple[datetime, float, float | None]],
    tz: ZoneInfo,
    *,
    balance_cool_c: float = 22.0,
    balance_heat_c: float = 15.0,
    min_bucket_hours: float = MIN_BUCKET_HOURS,
    min_temp_hours: int = MIN_TEMP_HOURS,
    half_life_days: float | None = RECENCY_HALF_LIFE_DAYS,
) -> LoadModel:
    """Fit a LoadModel from hourly (start, load_kw, temp_c | None) records.

    With enough joint load+temperature hours, ONLY those hours are used — this
    is the window cap: the bucket means and the regression see exactly the
    same data, and load history reaching further back than the temperature
    sensor's is excluded rather than mixed in. Without enough joint hours a
    base-only model is fitted on all records. Pure and synchronous for
    testability.

    The fit is recency-weighted (see RECENCY_HALF_LIFE_DAYS): bucket means,
    degree-hour means and the slope regression all decay a sample's weight by
    half every `half_life_days`, so the current season dominates without the
    older history being thrown away. None = unweighted (exact means, used by
    precision tests).
    """
    with_temp = [r for r in records if r[2] is not None]
    use_temp = len(with_temp) >= min_temp_hours
    rows = with_temp if use_temp else records
    if use_temp and len(with_temp) < len(records):
        joint_days = (with_temp[-1][0] - with_temp[0][0]).total_seconds() / 86400
        log.info(
            "learning window capped to load/temperature overlap: %d of %d "
            "hourly rows (~%.0f days)",
            len(with_temp),
            len(records),
            joint_days,
        )

    max_kw = max((max(r[1], 0.0) for r in rows), default=0.0)
    window_days = (
        (rows[-1][0] + timedelta(hours=1) - rows[0][0]).total_seconds() / 86400 if rows else 0.0
    )
    newest = max((r[0] for r in rows), default=None)

    def recency(start: datetime) -> float:
        if not half_life_days or newest is None:
            return 1.0
        age_days = (newest - start).total_seconds() / 86400
        return float(0.5 ** (age_days / half_life_days))

    # Two accumulators: the trust gate stays on RAW observed hours (two
    # weekend days must keep unlocking the weekend buckets, exactly as
    # before weighting existed), while the means use recency-weighted hours.
    raw_hours = np.zeros((2, 24))
    count = np.zeros((2, 24))  # effective (recency-weighted) observed hours
    load_sum = np.zeros((2, 24))
    cdh_sum = np.zeros((2, 24))
    hdh_sum = np.zeros((2, 24))
    pieces_per_row: list[tuple[list[tuple[int, int, float]], float]] = []
    for start, load_kw, temp_c in rows:
        rw = recency(start)
        pieces = _local_hour_pieces(start, start + timedelta(hours=1), tz)
        pieces_per_row.append((pieces, rw))
        for weekend, hour, w in pieces:
            raw_hours[weekend][hour] += w
            w *= rw
            count[weekend][hour] += w
            load_sum[weekend][hour] += max(load_kw, 0.0) * w
            if temp_c is not None:
                cdh_sum[weekend][hour] += max(temp_c - balance_cool_c, 0.0) * w
                hdh_sum[weekend][hour] += max(balance_heat_c - temp_c, 0.0) * w

    safe = np.where(count > 0, count, 1.0)
    base_arr = load_sum / safe
    model = LoadModel(
        base=[
            [
                float(base_arr[d][h]) if raw_hours[d][h] >= min_bucket_hours else None
                for h in range(24)
            ]
            for d in (0, 1)
        ],
        cdh_mean=cdh_sum / safe,
        hdh_mean=hdh_sum / safe,
        balance_cool_c=balance_cool_c,
        balance_heat_c=balance_heat_c,
        max_kw=max_kw,
        window_days=window_days,
        hours_used=len(rows),
        half_life_days=half_life_days or 0.0,
    )
    if not use_temp:
        return model

    # Weighted least squares by row scaling: multiplying each regression row
    # by sqrt(recency) makes the normal equations recency-weighted, matching
    # the bucket means the deviations are measured against.
    dc, dh, dy, hours = [], [], [], []
    temp_rows = 0
    for (_, load_kw, temp_c), (pieces, rw) in zip(rows, pieces_per_row, strict=True):
        sw = rw**0.5
        contributed = False
        for weekend, hour, _w in pieces:
            if raw_hours[weekend][hour] < min_bucket_hours:
                continue
            contributed = True
            dc.append(sw * (max(temp_c - balance_cool_c, 0.0) - model.cdh_mean[weekend][hour]))
            dh.append(sw * (max(balance_heat_c - temp_c, 0.0) - model.hdh_mean[weekend][hour]))
            dy.append(sw * (max(load_kw, 0.0) - base_arr[weekend][hour]))
            hours.append(hour)
        # count RECORDS, not regression rows: statistics rows are UTC-hour
        # aligned, which a half-hour-offset zone splits into two pieces —
        # counting pieces would double the dashboard's "fitted on N h".
        temp_rows += contributed
    if not dy:
        return model
    model.temp_hours = temp_rows
    dc_a, dh_a, dy_a = np.array(dc), np.array(dh), np.array(dy)
    cool, heat = _fit_slopes(dc_a, dh_a, dy_a)
    if max(cool, heat) > MAX_SLOPE_KW_PER_DEG:
        log.warning(
            "fitted temperature response is implausible (%.2f kW/°C cooling, "
            "%.2f kW/°C heating > %.1f max); dropping it — check the load "
            "sensor's units and data",
            cool,
            heat,
            MAX_SLOPE_KW_PER_DEG,
        )
        return model
    model.cool_kw_per_deg, model.heat_kw_per_deg = cool, heat
    # counts as a response even if the house turned out temperature-
    # insensitive (slopes 0): the data spoke, the answer was "flat"
    model.has_temp_response = True

    # Refine per hour block: the response is schedule-gated (heating at
    # breakfast, not at 3am) and the pooled slope dilutes it. Thin or
    # implausible blocks keep the pooled slope.
    hours_a = np.array(hours)
    cool_by_hour = [cool] * 24
    heat_by_hour = [heat] * 24
    for block in HOUR_BLOCKS:
        mask = np.isin(hours_a, block)
        if int(mask.sum()) < MIN_BLOCK_TEMP_HOURS:
            continue
        bc, bh = _fit_slopes(dc_a[mask], dh_a[mask], dy_a[mask])
        if max(bc, bh) > MAX_SLOPE_KW_PER_DEG:
            continue
        for h in block:
            cool_by_hour[h], heat_by_hour[h] = bc, bh
    model.cool_by_hour, model.heat_by_hour = cool_by_hour, heat_by_hour
    return model


def normalize_load_units(
    rows: list[tuple[datetime, float]], entity_id: str, source: str
) -> list[tuple[datetime, float]]:
    """Correct rows whose magnitude contradicts the declared unit, per UTC day.

    Trust the data over the label: statistics metadata keeps the unit from
    when the sensor first recorded, so it can lie after a unit change.
    Watt-magnitude values declared as kW inflate the forecast 1000x (and made
    the MILP infeasible live); kW-magnitude values declared as W silently
    forecast ~zero load. Per-day medians handle a window that mixes both
    regimes (sensor fixed mid-window): each day is corrected independently.
    """
    by_day: dict[object, list[float]] = {}
    for ts, v in rows:
        by_day.setdefault(ts.date(), []).append(v)
    scale_by_day: dict[object, float] = {}
    corrected = {"down": 0, "up": 0}
    for day, values in by_day.items():
        median = float(np.median(values))
        if median > MAX_PLAUSIBLE_MEDIAN_KW:
            scale_by_day[day] = 0.001
            corrected["down"] += 1
        elif 0 < median < MIN_PLAUSIBLE_MEDIAN_KW:
            scale_by_day[day] = 1000.0
            corrected["up"] += 1
        else:
            scale_by_day[day] = 1.0
    if corrected["down"] or corrected["up"]:
        log.warning(
            "%s %s magnitudes contradict the declared unit: rescaled %d day(s) "
            "W-as-kW and %d day(s) kW-as-W — fix the sensor's unit at the source",
            entity_id,
            source,
            corrected["down"],
            corrected["up"],
        )
    return [(ts, v * scale_by_day[ts.date()]) for ts, v in rows]


def _to_celsius(value: float, unit: str | None) -> float | None:
    if unit in ("°C", "C"):
        return value
    if unit in ("°F", "F"):
        return (value - 32.0) * 5.0 / 9.0
    return None


class HistoryLoadForecaster:
    """Learns household load from history, refreshed daily.

    Data source preference per refresh: hourly long-term statistics over the
    last HISTORY_DAYS (months-capable), then raw recorder history (~recorder
    purge window). Refresh is deliberately never fatal: on any failure the
    last learned model keeps serving (or zero load while status is "pending")
    and a retry happens after RETRY_INTERVAL.
    """

    def __init__(
        self,
        client: HaClient,
        entity_id: str,
        tz: ZoneInfo,
        *,
        temp_entity_id: str = "",
    ):
        self._client = client
        self._entity_id = entity_id
        self._temp_entity_id = temp_entity_id
        self._tz = tz
        self._days = HISTORY_DAYS
        self._model: LoadModel | None = None
        self._next_refresh: datetime | None = None
        self._source = ""
        self._learned_at: datetime | None = None

    @property
    def status(self) -> LoadForecastStatus:
        return "learned" if self._model is not None else "pending"

    @property
    def details(self) -> dict[str, Any]:
        model = self._model
        if model is None:
            return {}
        info: dict[str, Any] = {
            "load_entity": self._entity_id,
            "source": self._source,
            "window_days": round(model.window_days, 1),
            "hours_used": model.hours_used,
            "buckets": f"{model.known_buckets}/48",
            "temp_response": model.has_temp_response,
        }
        if model.half_life_days:
            info["half_life_days"] = round(model.half_life_days)
        if self._learned_at is not None:
            info["learned_at"] = self._learned_at.isoformat()
        if model.has_temp_response:
            info["temp_entity"] = self._temp_entity_id
            info["heat_kw_per_deg"] = round(
                max(model.heat_by_hour or [model.heat_kw_per_deg]), 3
            )
            info["cool_kw_per_deg"] = round(
                max(model.cool_by_hour or [model.cool_kw_per_deg]), 3
            )
            info["balance_heat_c"] = model.balance_heat_c
            info["balance_cool_c"] = model.balance_cool_c
            info["temp_hours"] = model.temp_hours
        return info

    async def refresh(self, now: datetime) -> None:
        if self._next_refresh and now < self._next_refresh:
            return
        # Arm the retry BEFORE learning: if the cycle timeout cancels a slow
        # learn, the next cycle must not repeat it at full cost immediately.
        self._next_refresh = now + RETRY_INTERVAL
        try:
            async with asyncio.timeout(LEARN_TIMEOUT_S):
                self._model = await self._learn(now)
            self._learned_at = now
            self._next_refresh = now + REFRESH_INTERVAL
            if self._model.has_temp_response:
                heat_peak = max(self._model.heat_by_hour or [self._model.heat_kw_per_deg])
                cool_peak = max(self._model.cool_by_hour or [self._model.cool_kw_per_deg])
                log.info(
                    "load model learned from %s: %d/48 hour buckets, temperature "
                    "response pooled %.3f/%.3f kW/°C cooling/heating "
                    "(peak hour block %.3f/%.3f)",
                    self._entity_id,
                    self._model.known_buckets,
                    self._model.cool_kw_per_deg,
                    self._model.heat_kw_per_deg,
                    cool_peak,
                    heat_peak,
                )
            else:
                log.info(
                    "load model learned from %s: %d/48 hour buckets covered, no "
                    "temperature response",
                    self._entity_id,
                    self._model.known_buckets,
                )
        except Exception as e:  # noqa: BLE001 - learned load is best-effort by design
            self._next_refresh = now + RETRY_INTERVAL
            if self._model is None:
                log.warning(
                    "could not learn load history from %s (%s); planning with "
                    "ZERO house load until learning succeeds — consider raising "
                    "battery.soc_min meanwhile",
                    self._entity_id,
                    e,
                )
            else:
                log.warning(
                    "could not refresh load history from %s (%s); keeping the "
                    "previous learned model",
                    self._entity_id,
                    e,
                )

    async def _learn(self, now: datetime) -> LoadModel:
        try:
            model = await self._learn_statistics(now)
            self._source = "statistics"
        except Exception as e:  # noqa: BLE001 - LTS is an upgrade, not a requirement
            log.info(
                "long-term statistics unavailable for %s (%s); falling back to "
                "recorder history",
                self._entity_id,
                e,
            )
            model = await self._learn_raw_history(now)
            self._source = "recorder history"
        if model.known_buckets == 0:
            raise ValueError("no hour bucket reached the minimum observed hours")
        return model

    async def _learn_statistics(self, now: datetime) -> LoadModel:
        """Learn from hourly LTS; the only path that can fit a temp response."""
        ids = [self._entity_id] + ([self._temp_entity_id] if self._temp_entity_id else [])
        units = await self._client.get_statistics_metadata(ids)
        if self._entity_id not in units:
            raise ValueError("no long-term statistics (sensor needs a state_class)")
        load_unit = units[self._entity_id]
        if load_unit not in _UNIT_TO_KW:
            raise ValueError(f"statistics unit {load_unit!r} is not W/kW")
        scale = _UNIT_TO_KW[load_unit]

        stats = await self._client.get_statistics(ids, now - timedelta(days=self._days), now)
        load_rows = stats.get(self._entity_id, [])
        if len(load_rows) < MIN_STATS_HOURS:
            raise ValueError(f"only {len(load_rows)} hourly statistics rows")

        temps: dict[datetime, float] = {}
        if self._temp_entity_id:
            if self._temp_entity_id in units:
                temp_unit = units[self._temp_entity_id]
                for ts, v in stats.get(self._temp_entity_id, []):
                    c = _to_celsius(v, temp_unit)
                    if c is not None:
                        temps[ts] = c
            if not temps:
                log.warning(
                    "outdoor_temp %s has no usable statistics; learning load "
                    "without a temperature response",
                    self._temp_entity_id,
                )
        scaled = normalize_load_units(
            [(ts, v * scale) for ts, v in load_rows], self._entity_id, "statistics"
        )
        records = [(ts, v, temps.get(ts)) for ts, v in scaled]
        return fit_load_model(records, self._tz)

    async def _learn_raw_history(self, now: datetime) -> LoadModel:
        state = await self._client.get_state(self._entity_id)
        unit = state.attributes.get("unit_of_measurement")
        if unit not in _UNIT_TO_KW:
            raise ValueError(f"{self._entity_id} unit {unit!r} is not W/kW")
        scale = _UNIT_TO_KW[unit]
        raw = await self._client.get_history(
            self._entity_id, now - timedelta(days=self._days), now
        )
        samples: list[tuple[datetime, float]] = []
        for ts, value in raw:
            try:
                samples.append((ts, float(value) * scale))
            except ValueError:
                continue  # unavailable/unknown gaps
        if len(samples) < 2:
            raise ValueError(f"not enough history samples ({len(samples)})")
        samples = normalize_load_units(samples, self._entity_id, "history")
        base = learn_hourly_profile(samples, self._tz)
        return LoadModel(
            base=base,
            cdh_mean=np.zeros((2, 24)),
            hdh_mean=np.zeros((2, 24)),
            max_kw=max((max(v, 0.0) for _, v in samples), default=0.0),
            window_days=(samples[-1][0] - samples[0][0]).total_seconds() / 86400,
        )

    def forecast(self, grid: TimeGrid, temps_c: np.ndarray | None) -> np.ndarray:
        if temps_c is not None and len(temps_c) != len(grid):
            raise ValueError(f"temps ({len(temps_c)}) != grid steps ({len(grid)})")
        model = self._model
        if model is None:
            return np.zeros(len(grid))
        out = np.empty(len(grid))
        for i, step in enumerate(grid.steps):
            weekend, hour = _step_bucket(step.start, step.end, self._tz)
            temp = float(temps_c[i]) if temps_c is not None else None
            out[i] = model.predict(weekend, hour, temp)
        return out


def build_load_forecaster(
    client: HaClient,
    load_power: str,
    tz: ZoneInfo,
    *,
    outdoor_temp: str = "",
) -> LoadForecaster:
    if not load_power:
        log.warning(
            "entities.load_power is not configured: load forecasting is "
            "unavailable and Numbat plans with ZERO house load. Configure a house "
            "load sensor; consider raising battery.soc_min meanwhile."
        )
        return UnconfiguredLoadForecaster()
    return HistoryLoadForecaster(client, load_power, tz, temp_entity_id=outdoor_temp)


def default_timezone(explicit: str = "") -> ZoneInfo:
    """Local timezone: `explicit` (NUMBAT_TZ, env or numbat/.env) wins, then the TZ
    env var (the Supervisor sets it for add-ons), then UTC.

    This zone anchors every local-time feature — learned hour-of-day buckets,
    the daily SoC target, vacation-mode end times — so the UTC fallback
    shifts all of them by the UTC offset: dev shells rarely export TZ, so set
    NUMBAT_TZ in numbat/.env (see .env.example). An invalid explicit zone fails
    loudly: it exists to remove ambiguity.
    """
    if explicit:
        try:
            return ZoneInfo(explicit)
        except (ValueError, ZoneInfoNotFoundError) as e:
            raise RuntimeError(f"NUMBAT_TZ={explicit!r} is not a valid IANA timezone") from e
    try:
        return ZoneInfo(os.environ["TZ"])
    except (KeyError, ZoneInfoNotFoundError):
        return ZoneInfo("UTC")
