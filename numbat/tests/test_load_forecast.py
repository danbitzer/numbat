from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from conftest import FakeHa, fake_ha_client

from numbat.forecast.load import (
    HistoryLoadForecaster,
    UnconfiguredLoadForecaster,
    build_load_forecaster,
    fit_load_model,
    learn_hourly_profile,
    normalize_load_units,
)
from numbat.timegrid import TimeGrid

ADELAIDE = ZoneInfo("Australia/Adelaide")


def half_hour_grid(start_utc: datetime, hours: int) -> TimeGrid:
    bounds = [start_utc + timedelta(minutes=30 * i) for i in range(1, hours * 2)]
    return TimeGrid.build(start_utc, bounds, timedelta(hours=hours))


def test_unconfigured_forecaster_is_zero_and_flagged():
    fc = build_load_forecaster(None, "", ADELAIDE)  # type: ignore[arg-type]
    assert isinstance(fc, UnconfiguredLoadForecaster)
    assert fc.status == "unconfigured"
    grid = half_hour_grid(datetime(2026, 7, 15, 0, 0, tzinfo=UTC), 1)
    assert np.allclose(fc.forecast(grid, None), 0.0)


def test_temp_length_mismatch_rejected():
    grid = half_hour_grid(datetime(2026, 7, 15, 0, 0, tzinfo=UTC), 1)
    fc = HistoryLoadForecaster(None, "sensor.load_power", ADELAIDE)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="grid steps"):
        fc.forecast(grid, np.array([20.0]))


# --- learned hourly profile (raw recorder history path) -----------------------

# 2026-07-13/14/15 are Mon/Tue/Wed; Adelaide winter is UTC+9:30, so local
# hour 10 on those days is 00:30–01:30 UTC.


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=ADELAIDE).astimezone(UTC)


def hour_of_samples(day: int, hour: int, kw: float) -> list[tuple[datetime, float]]:
    """Samples every 15 min covering exactly local [hour:00, hour+1:00)."""
    return [(local(day, hour, m), kw) for m in (0, 15, 30, 45)] + [(local(day, hour + 1), kw)]


def test_learn_time_weighted_mean():
    # 15 min at 4 kW then 30 min at 0.8 kW -> time-weighted, not sample mean
    samples = [
        (local(15, 10, 0), 4.0),
        (local(15, 10, 15), 0.8),
        (local(15, 10, 45), 0.0),
    ]
    learned = learn_hourly_profile(samples, ADELAIDE, min_bucket_hours=0.5)
    assert learned[0][10] == pytest.approx((4.0 * 0.25 + 0.8 * 0.5) / 0.75)
    assert learned[0][11] is None  # trailing sample has no successor


def test_learn_gap_capped_and_thin_buckets_none():
    # one sample then a 6h gap: only MAX_SEGMENT (30 min) counts
    samples = [(local(15, 10, 0), 2.0), (local(15, 16, 0), 2.0)]
    learned = learn_hourly_profile(samples, ADELAIDE, min_bucket_hours=0.5)
    assert learned[0][10] == pytest.approx(2.0)
    learned = learn_hourly_profile(samples, ADELAIDE, min_bucket_hours=1.0)
    assert learned[0][10] is None  # 0.5h observed < 1h required


def test_learn_weekday_weekend_split_and_negative_clamp():
    # 18th is a Saturday; negative readings clamp to zero
    samples = hour_of_samples(15, 10, 1.5) + hour_of_samples(18, 10, -0.5)
    learned = learn_hourly_profile(samples, ADELAIDE, min_bucket_hours=0.5)
    assert learned[0][10] == pytest.approx(1.5)
    assert learned[1][10] == pytest.approx(0.0)


def history_items(samples: list[tuple[datetime, float]]) -> list[dict]:
    return [{"last_changed": ts.isoformat(), "state": str(v)} for ts, v in samples]


def load_power_state(unit: str | None) -> dict:
    attrs = {"unit_of_measurement": unit} if unit else {}
    return {
        "entity_id": "sensor.load_power",
        "state": "1500",
        "attributes": attrs,
        "last_updated": "2026-07-15T01:55:00+00:00",
    }


NOW = datetime(2026, 7, 15, 2, 0, tzinfo=UTC)


async def test_raw_history_learning_with_bucket_mean_fallback():
    fake = FakeHa()  # no statistics -> raw recorder history path
    fake.states["sensor.load_power"] = load_power_state("W")
    # 3 weekdays × 1h of samples at local hour 10 -> 3h >= MIN_BUCKET_HOURS;
    # values in W (1500 -> 1.5 kW)
    samples = [s for day in (13, 14, 15) for s in hour_of_samples(day, 10, 1500.0)]
    fake.history["sensor.load_power"] = history_items(samples)
    async with fake_ha_client(fake) as client:
        fc = HistoryLoadForecaster(client, "sensor.load_power", ADELAIDE)
        assert fc.status == "pending"
        await fc.refresh(NOW)
        assert fc.status == "learned"
        # local hours 9,10,10,11: hour 10 is learned; thin buckets fall back
        # to the mean of the known buckets (1.5, the only one)
        grid = half_hour_grid(datetime(2026, 7, 15, 0, 0, tzinfo=UTC), 2)
        out = fc.forecast(grid, None)
    assert np.allclose(out, 1.5)


async def test_history_refresh_rate_limited():
    fake = FakeHa()
    fake.states["sensor.load_power"] = load_power_state("W")
    fake.history["sensor.load_power"] = history_items(
        [s for day in (13, 14, 15) for s in hour_of_samples(day, 10, 1000.0)]
    )
    async with fake_ha_client(fake) as client:
        fc = HistoryLoadForecaster(client, "sensor.load_power", ADELAIDE)
        await fc.refresh(NOW)
        await fc.refresh(NOW + timedelta(hours=7))
        assert len(fake.history_requests) == 1  # daily cadence: 7h is too soon
        await fc.refresh(NOW + timedelta(hours=25))
        assert len(fake.history_requests) == 2


async def test_history_failure_never_fatal_and_retries_later():
    fake = FakeHa()
    fake.states["sensor.load_power"] = load_power_state(None)  # unit missing
    async with fake_ha_client(fake) as client:
        fc = HistoryLoadForecaster(client, "sensor.load_power", ADELAIDE)
        await fc.refresh(NOW)  # must not raise
        assert fc.status == "pending"  # degraded: planning with zero load
        grid = half_hour_grid(datetime(2026, 7, 15, 0, 0, tzinfo=UTC), 1)
        assert np.allclose(fc.forecast(grid, None), 0.0)
        # empty history (recorder purged) is also non-fatal
        fake.states["sensor.load_power"] = load_power_state("kW")
        await fc.refresh(NOW + timedelta(minutes=5))  # rate-limited, no call yet
        await fc.refresh(NOW + timedelta(minutes=31))
        assert len(fake.history_requests) == 1  # retried after RETRY_INTERVAL
        assert fc.status == "pending"


# --- temperature response (long-term statistics) ------------------------------

# Synthetic truth: load = 0.4 kW + 0.1 kW per cooling degree above 22°C.
# Days alternate mild (18°C, cdh 0) and hot (28°C, cdh 6); Jul 13–17 are
# weekdays (18,28,18,28,18 -> bucket mean cdh 2.4, mean load 0.64 kW).

BASE_KW = 0.4
COOL_SLOPE = 0.1


def synth_day_temp(day: int) -> float:
    return 18.0 if (day - 13) % 2 == 0 else 28.0


def synth_records(days: range) -> list[tuple[datetime, float, float | None]]:
    out = []
    for day in days:
        t = synth_day_temp(day)
        kw = BASE_KW + COOL_SLOPE * max(t - 22.0, 0.0)
        out.extend((local(day, h), kw, t) for h in range(24))
    return out


def test_fit_recovers_cooling_slope_and_predicts_forecast_temps():
    # half_life_days=None: this test pins the exact unweighted means
    model = fit_load_model(synth_records(range(13, 20)), ADELAIDE, half_life_days=None)
    assert model.has_temp_response
    assert model.cool_kw_per_deg == pytest.approx(COOL_SLOPE)
    assert model.heat_kw_per_deg == pytest.approx(0.0)
    # weekday bucket: unbiased at bucket-typical temp, tracks forecast deviation
    # (26°C stays within the observed range, clear of the max_kw cap)
    assert model.base[0][10] == pytest.approx(0.64)
    assert model.predict(0, 10, 26.0) == pytest.approx(BASE_KW + COOL_SLOPE * 4)
    assert model.predict(0, 10, 20.0) == pytest.approx(BASE_KW)
    assert model.predict(0, 10, None) == pytest.approx(0.64)  # no temps -> bucket mean


def test_fit_without_enough_temp_hours_is_base_only():
    records = [(ts, kw, None) for ts, kw, _ in synth_records(range(13, 20))]
    records[0] = (records[0][0], records[0][1], 25.0)  # one joint hour is not enough
    model = fit_load_model(records, ADELAIDE)
    assert not model.has_temp_response
    assert model.cool_kw_per_deg == 0.0


def test_fit_window_capped_to_temperature_overlap():
    # 4 days of load WITH temperature at 2.0 kW, preceded by 6 days of
    # load-only history at 1.0 kW: the fit must use only the overlap, not
    # blend in load from before the temperature sensor existed.
    old = [(ts, 1.0, None) for ts, _, _ in synth_records(range(6, 12))]
    joint = [(ts, 2.0, 20.0) for ts, _, _ in synth_records(range(13, 17))]  # 96h >= 72
    model = fit_load_model(old + joint, ADELAIDE)
    assert model.has_temp_response
    assert model.base[0][10] == pytest.approx(2.0)


def test_fit_drops_implausible_temperature_response():
    # 5 kW per heating degree is beyond any house: the fit must refuse it
    # rather than hand the optimizer a triple-digit load forecast
    records = []
    for day in range(13, 20):
        t = 10.0 if (day - 13) % 2 == 0 else 20.0
        kw = 0.4 + 5.0 * max(15.0 - t, 0.0)
        records.extend((local(day, h), kw, t) for h in range(24))
    model = fit_load_model(records, ADELAIDE)
    assert not model.has_temp_response
    assert model.heat_kw_per_deg == 0.0


def test_block_slopes_capture_schedule_gated_heating():
    # Heating only runs 5-9am: 0.5 kW/°C there, nothing elsewhere. The
    # morning block must learn ~0.5 while the night block stays near zero —
    # a single pooled slope would smear this to ~0.1 everywhere.
    records = []
    for day in range(6, 20):  # 14 days so blocks clear MIN_BLOCK_TEMP_HOURS
        t = 10.0 if day % 2 == 0 else 20.0
        hdh = max(15.0 - t, 0.0)
        for h in range(24):
            kw = 0.4 + (0.5 * hdh if 5 <= h <= 9 else 0.0)
            records.append((local(day, h), kw, t))
    model = fit_load_model(records, ADELAIDE)
    assert model.has_temp_response
    assert model.heat_by_hour is not None
    assert model.heat_by_hour[6] == pytest.approx(0.5, abs=0.05)
    assert model.heat_by_hour[2] == pytest.approx(0.0, abs=0.05)
    # cold morning forecast: base(6am)=0.4+0.5*2.5=1.65, +0.5*(5-2.5) -> 2.9
    assert model.predict(0, 6, 10.0) == pytest.approx(2.9, abs=0.1)
    assert model.predict(0, 2, 10.0) == pytest.approx(0.4, abs=0.1)


def test_recency_weighting_tracks_a_seasonal_shift():
    """Heating season arrives: 83 days of 0.5 kW then three recent weeks of
    3.0 kW. The plain mean smears this to ~1.0 kW; the weighted fit must land
    much closer to the recent regime (the 2026-08-01 flat-battery morning was
    exactly this smear)."""
    records = []
    for day in range(104):
        kw = 3.0 if day >= 83 else 0.5
        start = datetime(2026, 3, 2, tzinfo=UTC) + timedelta(days=day)  # a Monday
        records.extend((start + timedelta(hours=h), kw, None) for h in range(24))
    weighted = fit_load_model(records, ADELAIDE)
    unweighted = fit_load_model(records, ADELAIDE, half_life_days=None)
    assert weighted.half_life_days > 0 and unweighted.half_life_days == 0
    h = 10
    assert unweighted.base[0][h] == pytest.approx(1.0, abs=0.05)  # the smear
    assert weighted.base[0][h] > 1.6  # recent regime dominates
    assert weighted.base[0][h] > unweighted.base[0][h] * 1.5


def test_recency_weighted_slope_fit_stays_exact_on_linear_data():
    # exact-linear synthetic data must recover the same slope weighted or not
    weighted = fit_load_model(synth_records(range(13, 20)), ADELAIDE)
    assert weighted.has_temp_response
    assert weighted.cool_kw_per_deg == pytest.approx(COOL_SLOPE)
    assert weighted.predict(0, 10, 26.0) == pytest.approx(BASE_KW + COOL_SLOPE * 4)
    # fit facts surfaced for the dashboard (temp_hours counts regression rows,
    # which exclude thin buckets — so bounded by, not equal to, the records)
    assert 0 < weighted.temp_hours <= len(synth_records(range(13, 20)))
    assert weighted.half_life_days == pytest.approx(21.0)


def test_predict_never_exceeds_observed_max():
    model = fit_load_model(synth_records(range(13, 20)), ADELAIDE)
    observed_max = BASE_KW + COOL_SLOPE * 6  # hottest synthetic day
    assert model.max_kw == pytest.approx(observed_max)
    # a 60°C forecast would extrapolate to ~4 kW; the cap holds it at max seen
    assert model.predict(0, 10, 60.0) == pytest.approx(observed_max)


def test_mixed_magnitude_window_corrected_per_day():
    # sensor emitted W-as-"kW" for a week, then was fixed to true kW: each
    # day is corrected independently, so neither regime poisons the other
    def week(first_day: int, kw: float) -> list[tuple[datetime, float]]:
        return [
            (datetime(2026, 7, d, h, tzinfo=UTC), kw)
            for d in range(first_day, first_day + 7)
            for h in range(24)
        ]

    rows = week(6, 400.0) + week(13, 0.4)  # mislabeled watts, then true kW
    fixed = normalize_load_units(rows, "sensor.load_power", "statistics")
    values = np.array([v for _, v in fixed])
    assert np.allclose(values, 0.4)
    # and the reverse lie (kW values under a W label, scaled /1000) recovers
    restored = normalize_load_units(week(13, 0.0004), "sensor.load_power", "statistics")
    assert np.allclose([v for _, v in restored], 0.4)


def test_utc_aligned_stats_rows_split_across_local_hours():
    # Real LTS rows start on UTC hour boundaries = local hh:30 in Adelaide.
    # A row covering local 09:30-10:30 must weight hours 9 AND 10, not be
    # dumped wholly into one bucket (which lagged the whole profile ~30 min).
    rows = []
    for day in (13, 14, 15):
        start_utc = local(day, 9, 30)  # local 09:30 == UTC hour boundary 00:00
        assert start_utc.minute == 0  # sanity: really UTC-hour aligned
        rows.append((start_utc, 2.0, None))
    model = fit_load_model(rows, ADELAIDE, min_bucket_hours=1.0)
    assert model.base[0][9] == pytest.approx(2.0)  # 1.5h observed
    assert model.base[0][10] == pytest.approx(2.0)  # 1.5h observed
    assert model.base[0][11] is None  # nothing leaked further


def stat_rows(values: list[tuple[datetime, float]]) -> list[dict]:
    return [{"start": int(ts.timestamp() * 1000), "mean": v} for ts, v in values]


async def test_lts_unit_mislabeled_as_kw_is_autocorrected():
    # Dan's live failure: statistics metadata claims kW but the values are
    # watt-magnitude (mkaiser load_power). Median >> any real house -> the
    # values must be treated as W, not taken at face value.
    fake = FakeHa()
    fake.statistics_meta = {"sensor.load_power": "kW"}
    records = synth_records(range(13, 20))
    fake.statistics = {
        "sensor.load_power": stat_rows([(ts, kw * 1000) for ts, kw, _ in records]),
    }
    async with fake_ha_client(fake) as client:
        fc = HistoryLoadForecaster(client, "sensor.load_power", ADELAIDE)
        await fc.refresh(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
        assert fc.status == "learned"
        grid = half_hour_grid(datetime(2026, 7, 15, 0, 0, tzinfo=UTC), 1)
        out = fc.forecast(grid, None)
    assert np.all(out < 2.0)  # kW-scale, not the 400+ "kW" the label implied


async def test_lts_learning_with_temperature_response():
    fake = FakeHa()
    fake.statistics_meta = {"sensor.load_power": "W", "sensor.outdoor_temp": "°C"}
    records = synth_records(range(13, 20))
    fake.statistics = {
        "sensor.load_power": stat_rows([(ts, kw * 1000) for ts, kw, _ in records]),
        "sensor.outdoor_temp": stat_rows([(ts, t) for ts, _, t in records]),
    }
    async with fake_ha_client(fake) as client:
        fc = HistoryLoadForecaster(
            client,
            "sensor.load_power",
            ADELAIDE,
            temp_entity_id="sensor.outdoor_temp",
        )
        await fc.refresh(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
        assert fc.status == "learned"
        assert len(fake.history_requests) == 0  # LTS path, no raw history needed
        # learn metadata surfaced to the dashboard
        info = fc.details
        assert info["source"] == "statistics"
        assert info["temp_response"] is True
        assert info["temp_entity"] == "sensor.outdoor_temp"
        assert info["window_days"] > 0
        assert info["hours_used"] > 0
        grid = half_hour_grid(datetime(2026, 7, 15, 0, 0, tzinfo=UTC), 1)
        hot = fc.forecast(grid, np.array([26.0, 26.0]))
        mild = fc.forecast(grid, np.array([20.0, 20.0]))
    # forecast temps drive the learned response
    assert np.allclose(hot, BASE_KW + COOL_SLOPE * 4)
    assert np.allclose(mild, BASE_KW)


async def test_lts_unavailable_falls_back_to_raw_history():
    fake = FakeHa()  # no statistics_meta: sensor has no state_class
    fake.states["sensor.load_power"] = load_power_state("W")
    samples = [s for day in (13, 14, 15) for s in hour_of_samples(day, 10, 1500.0)]
    fake.history["sensor.load_power"] = history_items(samples)
    async with fake_ha_client(fake) as client:
        fc = HistoryLoadForecaster(client, "sensor.load_power", ADELAIDE)
        await fc.refresh(NOW)
        assert len(fake.history_requests) == 1
        assert fc.details["source"] == "recorder history"
        assert fc.details["temp_response"] is False
        grid = half_hour_grid(datetime(2026, 7, 15, 0, 0, tzinfo=UTC), 1)
        out = fc.forecast(grid, None)
    assert out[1] == pytest.approx(1.5)  # learned from raw history


def test_default_timezone_precedence(monkeypatch):
    from numbat.forecast.load import default_timezone

    # NUMBAT_TZ > TZ env > UTC
    monkeypatch.setenv("TZ", "Australia/Sydney")
    assert str(default_timezone()) == "Australia/Sydney"
    assert str(default_timezone("Australia/Adelaide")) == "Australia/Adelaide"
    monkeypatch.delenv("TZ")
    assert str(default_timezone()) == "UTC"
    # an explicit-but-wrong zone fails loudly, it exists to remove ambiguity
    with pytest.raises(RuntimeError, match="NUMBAT_TZ"):
        default_timezone("Adelaide")
