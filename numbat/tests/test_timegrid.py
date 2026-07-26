from datetime import UTC, datetime, timedelta

import pytest
from conftest import load_fixture_state

from numbat.adapters.amber import parse_forecast_attribute
from numbat.models import Series
from numbat.timegrid import TimeGrid, coverage, resample_mean, resample_previous

NOW = datetime(2026, 7, 15, 11, 22, 0, tzinfo=UTC)  # 20:52 ACST, mid-interval


def fixture_series() -> Series:
    return parse_forecast_attribute(load_fixture_state("amber_express_feed_in_price"))


def test_grid_from_real_forecast_boundaries():
    series = fixture_series()
    grid = TimeGrid.build(NOW, series.times, timedelta(hours=36))

    # Fractional first step: now -> next 5-min forecast boundary
    assert grid.steps[0].start == NOW
    assert grid.steps[0].end == datetime(2026, 7, 15, 11, 25, 0, tzinfo=UTC)
    # 5-min native steps near-term (Dan's site is 5-min)
    assert grid.steps[1].end - grid.steps[1].start == timedelta(minutes=5)
    # 30-min steps beyond the 5-min window
    assert grid.steps[3].start.minute in {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
    step_widths = {s.end - s.start for s in grid.steps[10:-1]}
    assert step_widths == {timedelta(minutes=30)}
    # Grid ends exactly at the horizon
    assert grid.end == NOW + timedelta(hours=36)
    # Contiguous, no gaps
    for a, b in zip(grid.steps, grid.steps[1:], strict=False):
        assert a.end == b.start


def test_grid_pads_past_short_forecast():
    boundaries = [NOW + timedelta(minutes=30)]
    grid = TimeGrid.build(NOW, boundaries, timedelta(hours=4))
    assert grid.end == NOW + timedelta(hours=4)
    # padded with 30-min steps after the single boundary
    assert grid.steps[2].end - grid.steps[2].start == timedelta(minutes=30)


def test_grid_keeps_near_now_boundary():
    """A price boundary seconds away must NOT be merged into step 0 — that
    would price the following interval at the stale pre-boundary value (a
    spike starting <1min after an event-triggered re-solve would vanish)."""
    boundaries = [NOW + timedelta(seconds=30), NOW + timedelta(minutes=10)]
    grid = TimeGrid.build(NOW, boundaries, timedelta(hours=1))
    assert grid.steps[0].end == NOW + timedelta(seconds=30)
    assert grid.steps[1].end == NOW + timedelta(minutes=10)


def test_grid_drops_boundary_at_now():
    boundaries = [NOW + timedelta(microseconds=1), NOW + timedelta(minutes=10)]
    grid = TimeGrid.build(NOW, boundaries, timedelta(hours=1))
    assert grid.steps[0].end == NOW + timedelta(minutes=10)


def test_grid_requires_tz_aware_now():
    with pytest.raises(ValueError, match="tz-aware"):
        TimeGrid.build(datetime(2026, 7, 15), [], timedelta(hours=1))


def test_resample_previous_on_fixture():
    series = fixture_series()
    grid = TimeGrid.build(NOW, series.times, timedelta(hours=36))
    prices = resample_previous(series, grid)
    # Step 0 (11:22) holds the 11:20 (20:50 ACST) value: current interval price
    assert prices[0] == pytest.approx(0.1585)
    # Step 1 starts 11:25 -> 20:55 ACST entry
    assert prices[1] == pytest.approx(0.1403)
    # Tomorrow evening peak (20:00 ACST 16th = 10:30 UTC) present in resampled grid
    assert max(prices) == pytest.approx(0.6505)


def test_resample_mean_time_weighted():
    t0 = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    series = Series(
        times=[t0, t0 + timedelta(minutes=15), t0 + timedelta(minutes=30)],
        values=[1.0, 2.0, 4.0],
    )
    grid = TimeGrid.build(t0, [t0 + timedelta(minutes=30)], timedelta(minutes=45))
    out = resample_mean(series, grid, series_end=t0 + timedelta(minutes=45))
    assert out[0] == pytest.approx(1.5)  # 15min@1 + 15min@2 over 30min
    assert out[1] == pytest.approx(4.0)  # last segment holds


def test_coverage_reports_forecast_shortfall():
    series = fixture_series()
    short = TimeGrid.build(NOW, series.times, timedelta(hours=12))
    long = TimeGrid.build(NOW, series.times, timedelta(hours=72))
    assert coverage(series, short) == pytest.approx(1.0)
    assert coverage(series, long) < 0.5


def test_coverage_exact_span_is_full():
    """The last point covers one native interval — a series exactly spanning
    the grid must report 1.0, not fall short by an interval."""
    t0 = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    times = [t0 + timedelta(minutes=30 * i) for i in range(4)]  # covers 2h
    series = Series(times=times, values=[1.0] * 4)
    grid = TimeGrid.build(t0, times, timedelta(hours=2))
    assert coverage(series, grid) == pytest.approx(1.0)


def test_empty_series_rejected():
    with pytest.raises(ValueError, match="at least one point"):
        Series(times=[], values=[])
