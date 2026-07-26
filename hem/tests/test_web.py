import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_config import MINIMAL_CONFIG, make_settings

from hem.config_store import ConfigController, ConfigStore
from hem.models import Action, Plan, PlanInterval
from hem.web.app import AppState, create_app

NOW = datetime(2026, 7, 15, 11, 36, 30, tzinfo=UTC)


def sample_plan() -> Plan:
    iv = PlanInterval(
        start=NOW,
        end=NOW + timedelta(minutes=30),
        action=Action.DISCHARGE,
        power_kw=-3.2,
        soc_start=9.0,
        soc_end=7.3,
        buy=0.44,
        sell=0.16,
        pv_kw=0.0,
        load_kw=1.7,
        grid_import_kw=0.0,
        grid_export_kw=1.5,
        interval_cost=-0.12,
    )
    return Plan(
        intervals=[iv], objective_cost=-1.5, solver_status="optimal", solve_ms=18.0, computed_at=NOW
    )


def test_health_and_plan_endpoints():
    state = AppState()
    client = TestClient(create_app(state))

    assert client.get("/health").status_code == 200  # startup grace period
    assert client.get("/api/plan").status_code == 404

    state.plan = sample_plan()
    state.health.mark_success()
    body = client.get("/api/plan").json()
    assert body["solver_status"] == "optimal"
    assert body["intervals"][0]["action"] == "discharge"
    assert body["intervals"][0]["power_kw"] == -3.2


def test_dashboard_served_from_dist(tmp_path):
    # simulate a Vite build: index.html + a hashed asset
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(
        '<!doctype html><title>HEM</title><script src="./assets/index-abc.js"></script>'
    )
    (tmp_path / "assets" / "index-abc.js").write_text("console.log('hem')")
    client = TestClient(create_app(AppState(), dist_dir=tmp_path))
    index = client.get("/")
    assert index.status_code == 200
    assert "HEM" in index.text
    assert client.get("/assets/index-abc.js").status_code == 200
    # API routes registered before the mount still win
    assert client.get("/api/plan").status_code == 404


def test_cache_headers_html_revalidates_assets_immutable(tmp_path):
    # index.html must revalidate every load (an add-on update swaps the
    # hashed bundle it points at); the hashed assets can cache forever
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text('<script src="./assets/index-abc.js"></script>')
    (tmp_path / "assets" / "index-abc.js").write_text("x")
    client = TestClient(create_app(AppState(), dist_dir=tmp_path))
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "immutable" in client.get("/assets/index-abc.js").headers["cache-control"]
    assert "cache-control" not in client.get("/api/plan").headers  # untouched


def test_missing_dist_says_how_to_build(tmp_path):
    client = TestClient(create_app(AppState(), dist_dir=tmp_path / "nope"))
    resp = client.get("/")
    assert resp.status_code == 503
    assert "bun run build" in resp.json()["error"]


def make_controller(tmp_path, settings=None) -> ConfigController:
    store = ConfigStore(tmp_path / "hem-config.json")
    if settings is not None:
        store.save(settings)
    return ConfigController(store, settings)


def test_health_stays_healthy_while_disabled(tmp_path):
    state = AppState(lifecycle="unconfigured")
    # a year of no cycles would normally be unhealthy — but there are
    # deliberately no cycles, and the watchdog must not restart-loop us
    state.health.started_at = datetime(2025, 7, 1, tzinfo=UTC)
    client = TestClient(create_app(state))
    body = client.get("/health")
    assert body.status_code == 200
    assert body.json()["lifecycle"] == "unconfigured"


def test_health_grace_rearms_when_planning_resumes():
    # sat disabled for ages, then enabled: without re-arming the grace window
    # the watchdog would see 503 (stale last_success) the moment HEM starts
    state = AppState(lifecycle="disabled")
    state.health.started_at = datetime(2025, 7, 1, tzinfo=UTC)
    state.health.restart_grace()
    state.lifecycle = "running"
    assert TestClient(create_app(state)).get("/health").status_code == 200


def test_get_config_unconfigured(tmp_path):
    client = TestClient(create_app(AppState(), make_controller(tmp_path)))
    body = client.get("/api/config").json()
    assert body == {"configured": False, "lifecycle": "running", "config": None}


def test_put_config_persists_and_wakes_the_loop(tmp_path):
    controller = make_controller(tmp_path)
    client = TestClient(create_app(AppState(), controller))

    resp = client.put("/api/config", json={**MINIMAL_CONFIG, "enabled": True})
    assert resp.status_code == 200
    assert resp.json()["config"]["enabled"] is True
    assert controller.current is not None and controller.current.enabled is True
    assert controller.changed.is_set()  # main loop wakes and hot-applies
    on_disk = json.loads((tmp_path / "hem-config.json").read_text())
    assert on_disk["config"]["battery"]["capacity_kwh"] == 12.8

    body = client.get("/api/config").json()
    assert body["configured"] is True
    assert body["config"]["entities"]["battery_soc"] == "sensor.battery_level"


def test_put_config_invalid_returns_per_field_errors(tmp_path):
    controller = make_controller(tmp_path, make_settings())
    client = TestClient(create_app(AppState(), controller))

    bad = json.loads(json.dumps(MINIMAL_CONFIG))
    bad["battery"]["capacity_kwh"] = -1
    bad["entities"].pop("weather")
    resp = client.put("/api/config", json=bad)
    assert resp.status_code == 422
    locs = {err["loc"] for err in resp.json()["errors"]}
    assert "battery.capacity_kwh" in locs
    assert "entities.weather" in locs
    # nothing applied, nothing written
    assert controller.current.battery.capacity_kwh == 12.8
    assert not controller.changed.is_set()


def test_export_config_downloads_the_store_document(tmp_path):
    client = TestClient(create_app(AppState(), make_controller(tmp_path, make_settings())))

    resp = client.get("/api/config/export")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="hem-config-')
    assert disposition.endswith('.json"')
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["config"]["battery"]["capacity_kwh"] == 12.8

    # the roundtrip the Backup card relies on: an exported document's config
    # imports cleanly through the normal PUT validation path
    assert client.put("/api/config", json=body["config"]).status_code == 200


def test_export_config_unconfigured_is_409(tmp_path):
    client = TestClient(create_app(AppState(), make_controller(tmp_path)))
    assert client.get("/api/config/export").status_code == 409


class FakeStatesClient:
    async def list_states(self):
        from hem.ha.client import State

        now = datetime.now(UTC)
        return [
            State("sensor.load_power", "1.2", {"friendly_name": "Load power",
                                               "device_class": "power",
                                               "unit_of_measurement": "kW"}, now),
            State("weather.home", "sunny", {"friendly_name": "Home"}, now),
        ]


def test_entities_endpoint_lists_for_pickers():
    client = TestClient(create_app(AppState(), client=FakeStatesClient()))
    body = client.get("/api/entities").json()
    assert body["entities"] == [
        {"entity_id": "sensor.load_power", "name": "Load power", "domain": "sensor",
         "device_class": "power", "unit": "kW"},
        {"entity_id": "weather.home", "name": "Home", "domain": "weather",
         "device_class": None, "unit": None},
    ]
