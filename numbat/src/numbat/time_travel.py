"""Test mode, time travel: replay a past window from recorded HA history.

Given a historical instant, this rebuilds the optimizer's inputs from what the
recorder actually captured — Amber prices, house load, PV generation, battery
SoC — and runs the same solver the live loop uses. It answers "how would my
(current or overridden) settings have behaved across this real day?" without
waiting for the real data to change.

Honesty note baked into the response: these are recorded ACTUALS, i.e. perfect
hindsight — not the forecast Numbat saw at the time — so the replay shows how the
settings value the real prices, not a re-run of the historical decision.

Read-only like the synthetic scenarios: never publishes sensors or touches the
live plan. Reach limits: raw recorder history only (~10 days by default);
beyond that the response says so instead of silently padding.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from numbat.config import Settings
from numbat.forecast.load import normalize_load_units
from numbat.ha.client import HaClient
from numbat.models import Series
from numbat.simulate import simulate_solve
from numbat.timegrid import TimeGrid, coverage, resample_mean

# Fetch a little before `at` so the piecewise-constant series has a defined
# value at the very first step (hold-previous needs an earlier sample).
PRE_ROLL = timedelta(hours=1)
# The replay needs at least one full grid step of recorded data.
MIN_SPAN = timedelta(minutes=30)
# How far back to look for the last recorded SoC before `at`.
SOC_LOOKBACK = timedelta(hours=24)

RECORDER_HINT = (
    "no recorded prices around that time — Home Assistant's recorder keeps "
    "~10 days of history by default, so pick a more recent time (or raise "
    "recorder purge_keep_days)"
)


def _series(rows: list[tuple[datetime, str]], scale: float = 1.0) -> Series | None:
    """Recorder rows -> a piecewise-constant Series (None if no numeric data).

    Skips unavailable/unknown gaps; dedupes equal timestamps keeping the last
    value (Series requires strictly ascending times)."""
    points: dict[datetime, float] = {}
    for ts, state in rows:
        try:
            points[ts] = float(state) * scale
        except ValueError:
            continue
    if not points:
        return None
    times = sorted(points)
    return Series(times=times, values=[points[t] for t in times])


async def _power_series(
    client: HaClient, entity_id: str, start: datetime, end: datetime
) -> tuple[Series | None, str | None]:
    """History of a power sensor as a kW Series, unit-normalized.

    Returns (series, problem-note). The unit comes from the sensor's CURRENT
    state (history is fetched without attributes); a magnitude sanity check
    catches sensors whose label lies (the mkaiser load_power ships kW-labelled
    watt values — seen live)."""
    try:
        state = await client.get_state(entity_id)
        unit = (state.attributes.get("unit_of_measurement") or "").lower()
    except Exception as e:  # noqa: BLE001 - a dead sensor shouldn't kill the replay
        return None, f"{entity_id}: could not read the sensor ({e})"
    if unit == "w":
        scale = 0.001
    elif unit == "kw":
        scale = 1.0
    else:
        return None, f"{entity_id}: unit {unit!r} is not W/kW"
    rows = await client.get_history(entity_id, start, end)
    series = _series(rows, scale)
    if series is None:
        return None, f"{entity_id}: no recorded history in this window"
    # Trust magnitudes over the label, day by day (same guard the load
    # forecaster uses) — a watt value labelled kW inflates the replay 1000x.
    fixed = normalize_load_units(
        list(zip(series.times, series.values, strict=True)), entity_id, "history"
    )
    return Series(times=[t for t, _ in fixed], values=[v for _, v in fixed]), None


async def _recorded_soc_frac(
    client: HaClient, entity_id: str, at: datetime
) -> float | None:
    """The last recorded battery SoC at or before `at`, as a fraction."""
    rows = await client.get_history(entity_id, at - SOC_LOOKBACK, at + timedelta(minutes=1))
    last: float | None = None
    for ts, state in rows:
        if ts > at:
            break
        try:
            last = float(state)
        except ValueError:
            continue
    if last is None:
        return None
    # Recorder history carries no units; a value above 1.5 is unambiguously a
    # percentage (matching the live adapter's heuristic).
    frac = last / 100.0 if last > 1.5 else last
    return float(np.clip(frac, 0.0, 1.0))


async def run_history_simulation(
    settings: Settings,
    client: HaClient,
    *,
    at: datetime,
    soc_frac: float | None,
    wall_now: datetime,
    tz: ZoneInfo,
) -> dict:
    """Replay the optimizer over recorded history starting at `at`.

    soc_frac None means "use the battery level recorded at that time".
    Raises ValueError with a user-facing message for anything the UI should
    show as a validation problem (bad time, no data)."""
    if at.tzinfo is None:
        # datetime-local inputs submit naive local time; anchor it to Numbat's zone
        at = at.replace(tzinfo=tz)
    at = at.astimezone(UTC)
    if at >= wall_now - MIN_SPAN:
        raise ValueError(
            "pick a time at least 30 minutes in the past — the replay needs "
            "recorded data after it"
        )

    # The horizon can only reach as far as recorded reality does.
    configured = timedelta(hours=settings.optimizer.horizon_hours)
    horizon = min(configured, wall_now - at)
    end = at + horizon
    ent = settings.entities

    coros: dict[str, object] = {
        "buy": client.get_history(ent.buy_price, at - PRE_ROLL, end),
        "sell": client.get_history(ent.sell_price, at - PRE_ROLL, end),
    }
    if ent.load_power:
        coros["load"] = _power_series(client, ent.load_power, at - PRE_ROLL, end)
    if ent.pv_power:
        coros["pv"] = _power_series(client, ent.pv_power, at - PRE_ROLL, end)
    if soc_frac is None:
        coros["soc"] = _recorded_soc_frac(client, ent.battery_soc, at)
    # gather (not TaskGroup): a failure propagates as itself, not wrapped in an
    # ExceptionGroup the endpoint would render as an opaque message.
    fetched = dict(
        zip(coros.keys(), await asyncio.gather(*coros.values()), strict=True)
    )

    buy_series = _series(fetched["buy"])
    sell_series = _series(fetched["sell"])
    if buy_series is None or sell_series is None:
        raise ValueError(RECORDER_HINT)

    notes = [
        "Replaying recorded actuals — the prices, solar and load are what "
        "really happened, not the forecast Numbat saw at the time."
    ]
    if horizon < configured:
        hours = horizon.total_seconds() / 3600
        notes.append(
            f"Horizon clamped to {hours:.1f}h — recorded data only reaches the present."
        )

    # NEM-aligned 30-min boundaries (Amber settles on :00/:30), fractional
    # first step from `at` to the next boundary, exactly like the live grid.
    first = at.replace(minute=0 if at.minute < 30 else 30, second=0, microsecond=0)
    boundaries = []
    b = first
    while b < end:
        b += timedelta(minutes=30)
        boundaries.append(b)
    grid = TimeGrid.build(at, boundaries, horizon)

    # Time-weighted means: prices are 5-min settlements under a 30-min grid, so
    # the mean is the economically-correct per-step price (a 5-min spike inside
    # a step is diluted accordingly — inherent to the grid, not a bug).
    buy = resample_mean(buy_series, grid)
    sell = resample_mean(sell_series, grid)
    for name, series in (("buy price", buy_series), ("feed-in price", sell_series)):
        cov = coverage(series, grid)
        if cov < 0.95:
            notes.append(
                f"Recorded {name} covers only {cov:.0%} of the window — the "
                "rest holds the nearest value."
            )

    sources = {"prices": "recorded"}
    if "load" in fetched:
        load_series, problem = fetched["load"]
        if load_series is not None:
            load = resample_mean(load_series, grid)
            sources["load"] = "recorded"
        else:
            load = np.zeros(len(grid))
            sources["load"] = "none"
            notes.append(f"House load unavailable ({problem}) — replaying with zero load.")
    else:
        load = np.zeros(len(grid))
        sources["load"] = "none"
        notes.append("No house load sensor configured — replaying with zero load.")

    if "pv" in fetched:
        pv_series, problem = fetched["pv"]
        if pv_series is not None:
            pv = np.clip(resample_mean(pv_series, grid), 0.0, None)
            sources["pv"] = "recorded"
        else:
            pv = np.zeros(len(grid))
            sources["pv"] = "none"
            notes.append(f"PV history unavailable ({problem}) — replaying with zero solar.")
    else:
        pv = np.zeros(len(grid))
        sources["pv"] = "none"
        notes.append(
            "No PV power sensor configured (Settings → Entities → PV power) — "
            "replaying with zero solar."
        )

    if soc_frac is None:
        recorded = fetched["soc"]
        if recorded is None:
            raise ValueError(
                "no recorded battery level at that time — set the starting "
                "SoC manually instead"
            )
        soc_frac = recorded
        sources["soc"] = "recorded"
    else:
        sources["soc"] = "manual"

    meta_extra = {
        "mode": "history",
        "at": at.isoformat(),
        "soc_frac": round(float(soc_frac), 3),
        "sources": sources,
        "notes": notes,
    }
    # The solve is pure CPU (tens of ms) — off the event loop like the
    # synthetic path.
    return await asyncio.to_thread(
        simulate_solve,
        settings,
        grid=grid,
        buy=buy,
        sell=sell,
        pv=pv,
        load=load,
        soc_frac=soc_frac,
        tz=tz,
        meta_extra=meta_extra,
    )
