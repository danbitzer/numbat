"""Optimization time grid and resampling.

The grid is data-driven: built from the price forecast's own interval
boundaries (5-min near-term then 30-min on Dan's site; pure 30-min on
30-min sites), with a fractional first step from `now` to the next
boundary, padded with fixed-width steps out to the horizon when the
forecast is shorter.

Everything is tz-aware UTC. Local time never enters this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from numbat.models import Series

PAD_STEP = timedelta(minutes=30)
# Only boundaries essentially AT `now` are dropped. Dropping anything larger
# merges the next price interval into step 0 at the stale pre-boundary price —
# a spike starting <1min after an event-triggered re-solve would vanish from
# the plan. A sub-minute first step is harmless to the MILP.
MIN_STEP = timedelta(seconds=1)


@dataclass(frozen=True)
class GridStep:
    start: datetime
    end: datetime

    @property
    def dt_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


@dataclass(frozen=True)
class TimeGrid:
    steps: tuple[GridStep, ...]

    def __len__(self) -> int:
        return len(self.steps)

    @property
    def start(self) -> datetime:
        return self.steps[0].start

    @property
    def end(self) -> datetime:
        return self.steps[-1].end

    @property
    def dt_hours(self) -> np.ndarray:
        return np.array([s.dt_hours for s in self.steps])

    @classmethod
    def build(
        cls,
        now: datetime,
        boundaries: list[datetime],
        horizon: timedelta,
        pad_step: timedelta = PAD_STEP,
    ) -> TimeGrid:
        """Build a grid from `now` to `now + horizon` using forecast boundaries.

        `boundaries` are forecast interval starts (any order, duplicates ok).
        Boundaries within MIN_STEP of `now` are dropped so the first step is
        never degenerate; steps beyond the last boundary are padded at
        `pad_step` width, with the final step clamped to the horizon end.
        """
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware")
        end = now + horizon
        cuts = sorted({b for b in boundaries if now + MIN_STEP <= b < end})
        last = cuts[-1] if cuts else now
        while last + pad_step < end:
            last += pad_step
            cuts.append(last)
        edges = [now, *cuts, end]
        return cls(
            tuple(GridStep(a, b) for a, b in zip(edges, edges[1:], strict=False))
        )


def resample_previous(series: Series, grid: TimeGrid) -> np.ndarray:
    """Value at each step start, 'previous' interpolation (step-hold).

    Steps before the series start hold the first value; steps after the last
    point hold the last value. Use `coverage()` to detect how much of the grid
    the series actually covers.
    """
    out = np.empty(len(grid))
    times = series.times
    j = 0
    for i, step in enumerate(grid.steps):
        while j + 1 < len(times) and times[j + 1] <= step.start:
            j += 1
        out[i] = series.values[j]
    return out


def resample_mean(series: Series, grid: TimeGrid, series_end: datetime | None = None) -> np.ndarray:
    """Time-weighted mean over each step of the piecewise-constant series.

    The last series value holds until `series_end` (default: last point plus
    the median native interval). Time outside the series' span contributes the
    nearest value (hold-first/hold-last), keeping results sane at the edges;
    check `coverage()` before trusting steps far outside the span.
    """
    times = series.times
    if series_end is None:
        series_end = times[-1] + _native_interval(series)

    # Segment edges and values covering (-inf, +inf) via hold-first/hold-last.
    edges = [t.timestamp() for t in times] + [series_end.timestamp()]
    vals = list(series.values)

    out = np.empty(len(grid))
    for i, step in enumerate(grid.steps):
        t0, t1 = step.start.timestamp(), step.end.timestamp()
        acc = 0.0
        # before first edge
        if t0 < edges[0]:
            acc += (min(t1, edges[0]) - t0) * vals[0]
        # inside segments
        for k in range(len(vals)):
            a, b = max(t0, edges[k]), min(t1, edges[k + 1])
            if b > a:
                acc += (b - a) * vals[k]
        # after last edge
        if t1 > edges[-1]:
            acc += (t1 - max(t0, edges[-1])) * vals[-1]
        out[i] = acc / (t1 - t0)
    return out


def _native_interval(series: Series) -> timedelta:
    if len(series.times) > 1:
        deltas = sorted(b - a for a, b in zip(series.times, series.times[1:], strict=False))
        return deltas[len(deltas) // 2]
    return PAD_STEP


def coverage(series: Series, grid: TimeGrid) -> float:
    """Fraction of the grid span covered by the series' span.

    The last point covers one native interval past its start (matching
    resample_mean's series_end), so a forecast that exactly spans the grid
    reports 1.0 rather than being short by one interval.
    """
    series_end = series.end + _native_interval(series)
    a = max(grid.start, series.start)
    b = min(grid.end, series_end)
    total = (grid.end - grid.start).total_seconds()
    return max(0.0, (b - a).total_seconds()) / total if total else 0.0
