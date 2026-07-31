"""Time travel: replaying recorded HA history through the optimizer."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from test_config import make_settings
from test_simulate import _controller

from numbat.ha.client import EntityNotFoundError, State
from numbat.time_travel import normalize_pv_units, run_history_simulation
from numbat.web.app import AppState, create_app

ADELAIDE = ZoneInfo("Australia/Adelaide")
WALL_NOW = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
AT = WALL_NOW - timedelta(hours=6)

ENTITIES = {
    "buy_price": "sensor.amber_buy",
    "sell_price": "sensor.amber_sell",
    "pv_forecast_today": "sensor.pv_today",
    "pv_forecast_tomorrow": "sensor.pv_tomorrow",
    "battery_soc": "sensor.soc",
    "battery_power": "sensor.battery_power",
    "weather": "weather.home",
    "load_power": "sensor.load",
    "pv_power": "sensor.pv_actual",
}


class FakeHistoryClient:
    """Duck-typed HaClient surface for the replay: history + current state."""

    def __init__(self) -> None:
        self.history: dict[str, list[tuple[datetime, str]]] = {}
        self.states: dict[str, State] = {}

    async def get_history(self, entity_id, start, end):
        return [
            (ts, v) for ts, v in self.history.get(entity_id, []) if start <= ts <= end
        ]

    async def get_state(self, entity_id):
        if entity_id not in self.states:
            raise EntityNotFoundError(entity_id)
        return self.states[entity_id]


def stocked_client() -> FakeHistoryClient:
    """A recorder with 8h of data ending at WALL_NOW: half-hourly prices
    (cheap dip in the middle), kW load, W-labelled PV, and one SoC sample."""
    fake = FakeHistoryClient()
    t0 = AT - timedelta(hours=1)
    buy, sell, load, pv = [], [], [], []
    for i in range(16):  # 8h of 30-min rows
        ts = t0 + timedelta(minutes=30 * i)
        cheap = 6 <= i < 8  # a one-hour cheap window mid-replay
        buy.append((ts, "0.05" if cheap else "0.30"))
        sell.append((ts, "0.02" if cheap else "0.10"))
        load.append((ts, "0.8"))
        pv.append((ts, "3000"))  # W-labelled sensor
    fake.history[ENTITIES["buy_price"]] = buy
    fake.history[ENTITIES["sell_price"]] = sell
    fake.history[ENTITIES["load_power"]] = load
    fake.history[ENTITIES["pv_power"]] = pv
    fake.history[ENTITIES["battery_soc"]] = [(AT - timedelta(minutes=10), "80")]
    fake.states[ENTITIES["load_power"]] = State(
        ENTITIES["load_power"], "0.8", {"unit_of_measurement": "kW"}, WALL_NOW
    )
    fake.states[ENTITIES["pv_power"]] = State(
        ENTITIES["pv_power"], "3000", {"unit_of_measurement": "W"}, WALL_NOW
    )
    return fake


async def test_replay_builds_inputs_from_history_and_solves():
    settings = make_settings(entities=ENTITIES)
    result = await run_history_simulation(
        settings, stocked_client(), at=AT, soc_frac=None, wall_now=WALL_NOW, tz=ADELAIDE
    )
    assert result["solver_status"].startswith("optimal")
    meta = result["meta"]
    assert meta["simulated"] is True and meta["mode"] == "history"
    assert meta["at"] == AT.isoformat()
    assert meta["soc_frac"] == pytest.approx(0.8)  # recorded 80%
    assert meta["sources"] == {
        "prices": "recorded", "load": "recorded", "pv": "recorded", "soc": "recorded",
    }
    # horizon clamped from 36h to the 6h of data that exists, and says so
    assert any("clamped" in n for n in meta["notes"])
    assert any("hindsight" in n or "actual" in n for n in meta["notes"])
    ivs = result["intervals"]
    assert 11 <= len(ivs) <= 13  # ~6h of 30-min steps
    # replayed values, not synthetic: 0.30 buy, 0.8 kW load, 3 kW PV (W-scaled)
    assert ivs[0]["buy"] == pytest.approx(0.30)
    assert ivs[0]["load_kw"] == pytest.approx(0.8)
    assert ivs[0]["pv_kw"] == pytest.approx(3.0)
    # the recorded cheap window made it into the replay
    assert min(iv["buy"] for iv in ivs) == pytest.approx(0.05)


def test_normalize_pv_units_never_inflates_a_dark_day():
    """The 2026-08-01 live regression: a replay's first UTC day held only the
    dusk tail + night (row median ~1 W), so the load guard's median heuristic
    scaled that day x1000, inflating the next morning's ramp into phantom
    megawatts the optimizer then curtailed. PV's guard keys on the day's PEAK
    and never scales up: honest kW must pass through untouched."""
    dusk = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)  # UTC day 1: dusk into night
    ramp = datetime(2026, 7, 31, 0, 30, tzinfo=UTC)  # UTC day 2: real generation
    rows = [
        (dusk + timedelta(minutes=5 * i), v)
        for i, v in enumerate([0.329, 0.2, 0.05, 0.01, 0.001, 0.0, 0.0, 0.001])
    ] + [
        (ramp + timedelta(minutes=30 * i), v)
        for i, v in enumerate([0.5, 3.9, 6.3, 9.1, 7.1, 3.7, 1.0, 0.0])
    ]
    assert normalize_pv_units(rows, "sensor.pv_actual") == rows


def test_normalize_pv_units_scales_down_watt_magnitudes():
    # a kW-labelled sensor shipping watt values: the peak gives it away
    day = datetime(2026, 7, 31, 0, 30, tzinfo=UTC)
    rows = [
        (day + timedelta(minutes=30 * i), v)
        for i, v in enumerate([0.0, 3940.0, 9114.0, 1084.0])
    ]
    fixed = [v for _, v in normalize_pv_units(rows, "sensor.pv_actual")]
    assert fixed == pytest.approx([0.0, 3.94, 9.114, 1.084])


async def test_replay_pv_dark_stretch_is_not_inflated():
    """End-to-end wiring: recorded PV that is mostly standby watts with a
    short real ramp must reach the replay at real magnitude."""
    settings = make_settings(entities=ENTITIES)
    fake = stocked_client()
    t0 = AT - timedelta(hours=1)
    fake.history[ENTITIES["pv_power"]] = [
        (t0 + timedelta(minutes=30 * i), "1") for i in range(12)
    ] + [
        (t0 + timedelta(minutes=30 * (12 + i)), w)
        for i, w in enumerate(["2000", "4000", "3000", "500"])
    ]
    result = await run_history_simulation(
        settings, fake, at=AT, soc_frac=0.5, wall_now=WALL_NOW, tz=ADELAIDE
    )
    peak = max(iv["pv_kw"] for iv in result["intervals"])
    assert peak == pytest.approx(4.0, abs=0.5)


async def test_manual_soc_override_wins_over_recorded():
    settings = make_settings(entities=ENTITIES)
    result = await run_history_simulation(
        settings, stocked_client(), at=AT, soc_frac=0.25, wall_now=WALL_NOW, tz=ADELAIDE
    )
    assert result["meta"]["soc_frac"] == pytest.approx(0.25)
    assert result["meta"]["sources"]["soc"] == "manual"


async def test_naive_at_is_interpreted_in_local_time():
    settings = make_settings(entities=ENTITIES)
    naive = AT.astimezone(ADELAIDE).replace(tzinfo=None)
    result = await run_history_simulation(
        settings, stocked_client(), at=naive, soc_frac=0.5, wall_now=WALL_NOW, tz=ADELAIDE
    )
    assert result["meta"]["at"] == AT.isoformat()


async def test_no_recorded_prices_is_a_clear_error():
    settings = make_settings(entities=ENTITIES)
    fake = stocked_client()
    fake.history[ENTITIES["buy_price"]] = []
    with pytest.raises(ValueError, match="recorder"):
        await run_history_simulation(
            settings, fake, at=AT, soc_frac=0.5, wall_now=WALL_NOW, tz=ADELAIDE
        )


async def test_too_recent_at_is_rejected():
    settings = make_settings(entities=ENTITIES)
    with pytest.raises(ValueError, match="past"):
        await run_history_simulation(
            settings,
            stocked_client(),
            at=WALL_NOW - timedelta(minutes=5),
            soc_frac=0.5,
            wall_now=WALL_NOW,
            tz=ADELAIDE,
        )


async def test_missing_pv_sensor_replays_zero_solar_with_a_note():
    entities = {k: v for k, v in ENTITIES.items() if k != "pv_power"}
    settings = make_settings(entities=entities)
    result = await run_history_simulation(
        settings, stocked_client(), at=AT, soc_frac=0.5, wall_now=WALL_NOW, tz=ADELAIDE
    )
    assert result["meta"]["sources"]["pv"] == "none"
    assert any("PV power sensor" in n for n in result["meta"]["notes"])
    assert all(iv["pv_kw"] == 0.0 for iv in result["intervals"])


async def test_no_recorded_soc_asks_for_manual():
    settings = make_settings(entities=ENTITIES)
    fake = stocked_client()
    fake.history[ENTITIES["battery_soc"]] = []
    with pytest.raises(ValueError, match="manually"):
        await run_history_simulation(
            settings, fake, at=AT, soc_frac=None, wall_now=WALL_NOW, tz=ADELAIDE
        )


# ---- the /api/simulate/history endpoint -------------------------------------


def freshly_stocked_client() -> FakeHistoryClient:
    """Endpoint tests run against the real wall clock — restock relative to it."""
    fake = stocked_client()
    shift = datetime.now(UTC) - WALL_NOW
    for entity, rows in fake.history.items():
        fake.history[entity] = [(ts + shift, v) for ts, v in rows]
    return fake


def test_endpoint_happy_path(tmp_path: Path):
    settings = make_settings(entities=ENTITIES, enabled=True)
    app = create_app(
        AppState(), _controller(tmp_path, settings), client=freshly_stocked_client()
    )
    at = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    resp = TestClient(app).post("/api/simulate/history", json={"at": at})
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["meta"]["mode"] == "history"
    assert body["solver_status"].startswith("optimal")


def test_endpoint_requires_ha_client(tmp_path: Path):
    settings = make_settings(entities=ENTITIES, enabled=True)
    app = create_app(AppState(), _controller(tmp_path, settings), client=None)
    resp = TestClient(app).post(
        "/api/simulate/history", json={"at": "2026-07-20T12:00:00+00:00"}
    )
    assert resp.status_code == 503


def test_endpoint_requires_config(tmp_path: Path):
    app = create_app(AppState(), _controller(tmp_path), client=FakeHistoryClient())
    resp = TestClient(app).post(
        "/api/simulate/history", json={"at": "2026-07-20T12:00:00+00:00"}
    )
    assert resp.status_code == 409


def test_endpoint_rejects_bad_datetime(tmp_path: Path):
    settings = make_settings(entities=ENTITIES, enabled=True)
    app = create_app(
        AppState(), _controller(tmp_path, settings), client=FakeHistoryClient()
    )
    resp = TestClient(app).post("/api/simulate/history", json={"at": "not-a-time"})
    assert resp.status_code == 400
    assert "ISO datetime" in resp.json()["error"]
