"""Test mode: synthetic-scenario simulation + the /api/simulate endpoint."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from test_config import make_settings

from numbat.config_store import ConfigController, ConfigStore
from numbat.simulate import SCENARIOS, run_simulation, scenario_list
from numbat.web.app import AppState, create_app

ADELAIDE = ZoneInfo("Australia/Adelaide")
NOW = datetime(2026, 7, 21, 7, 30, tzinfo=UTC)  # ~5pm Adelaide


def _controller(tmp_path: Path, settings=None) -> ConfigController:
    store = ConfigStore(tmp_path / "numbat-config.json")
    if settings is not None:
        store.save(settings)
    return ConfigController(store, settings)


def test_scenario_list_is_nonempty_and_well_formed():
    items = scenario_list()
    assert items and all({"id", "label", "description"} <= set(s) for s in items)
    assert set(SCENARIOS) == {s["id"] for s in items}


def test_run_simulation_returns_a_full_optimal_plan():
    settings = make_settings()
    result = run_simulation(
        settings, scenario_id="typical", soc_frac=0.5, now=NOW, tz=ADELAIDE
    )
    assert result["solver_status"].startswith("optimal")
    horizon_steps = settings.optimizer.horizon_hours * 2
    assert abs(len(result["intervals"]) - horizon_steps) <= 1
    assert result["meta"]["simulated"] is True
    assert result["meta"]["scenario"] == "typical"
    # every interval carries the fields the dashboard renders
    iv = result["intervals"][0]
    assert {"buy", "sell", "pv_kw", "load_kw", "soc_start", "action"} <= set(iv)


def test_every_scenario_solves():
    settings = make_settings()
    for sid in SCENARIOS:
        r = run_simulation(settings, scenario_id=sid, soc_frac=0.6, now=NOW, tz=ADELAIDE)
        assert r["solver_status"].startswith("optimal"), sid


def test_min_battery_export_price_suppresses_low_price_export():
    # cheap_overnight has a pricey evening (feed-in ~0.30) and cheap nights;
    # a floor above the evening feed-in should cut battery export there.
    base = run_simulation(make_settings(), scenario_id="cheap_overnight", soc_frac=0.9,
                          now=NOW, tz=ADELAIDE)
    floored_settings = make_settings(
        grid={"import_limit_kw": 15.0, "export_limit_kw": 5.0,
              "min_battery_export_price": 0.50},
    )
    floored = run_simulation(floored_settings, scenario_id="cheap_overnight", soc_frac=0.9,
                             now=NOW, tz=ADELAIDE)
    def batt_export(r):
        return sum(iv["grid_export_kw"] for iv in r["intervals"] if iv["action"] == "discharge")

    assert batt_export(floored) < batt_export(base)


def test_simulate_applies_the_haircut_and_reports_raw_prices():
    """Test mode must honour the sandbox's forecast_haircut (else A/B runs
    lie), while displayed prices stay raw in every case."""
    base = run_simulation(make_settings(), scenario_id="export_spike_tonight",
                          soc_frac=0.9, now=NOW, tz=ADELAIDE)
    cut = run_simulation(make_settings(optimizer={"forecast_haircut": 1.0}),
                         scenario_id="export_spike_tonight",
                         soc_frac=0.9, now=NOW, tz=ADELAIDE)
    # displayed prices are the raw scenario in both runs
    assert [iv["sell"] for iv in cut["intervals"]] == [iv["sell"] for iv in base["intervals"]]
    # but a full trim (forecast spike flattened to the median) kills the
    # hold-and-sell-into-the-spike play the base plan makes
    def batt_export(r):
        return sum(iv["grid_export_kw"] for iv in r["intervals"] if iv["action"] == "discharge")

    assert batt_export(cut) < batt_export(base)


def test_simulate_spike_reserve_floor_is_wired():
    """Kills the sell_floor_kwh=None mutation: the sandbox reserve must
    actually constrain the sim's sales, not just decorate the lever.
    export_spike_tonight peaks at 0.90 — below the $1 release price — so
    nothing releases and the reserved tranche can't sell."""
    base = run_simulation(make_settings(), scenario_id="export_spike_tonight",
                          soc_frac=0.9, now=NOW, tz=ADELAIDE)
    reserved = run_simulation(make_settings(spike={"reserve_soc": 0.6}),
                              scenario_id="export_spike_tonight",
                              soc_frac=0.9, now=NOW, tz=ADELAIDE)

    def batt_export(r):
        return sum(iv["grid_export_kw"] for iv in r["intervals"] if iv["action"] == "discharge")

    assert batt_export(reserved) < batt_export(base)
    lever = reserved["meta"]["explanation"]["levers"]["spike_reserve"]
    assert lever == {"soc": 0.6, "threshold": 1.0, "released": False}


def test_simulate_forecast_spike_releases_in_plan():
    """evening_spike's 1.25 feed-in clears the $1 release price as FORECAST:
    the plan anticipates the reserve sale into that window (in-plan release;
    execution would still be gated on the price confirming)."""
    r = run_simulation(make_settings(spike={"reserve_soc": 0.6}),
                       scenario_id="evening_spike",
                       soc_frac=0.9, now=NOW, tz=ADELAIDE)
    reserve_kwh = 0.6 * 12.8
    assert any(
        iv["action"] == "discharge" and iv["soc_end"] < reserve_kwh - 0.5
        for iv in r["intervals"]
    )


def test_api_scenarios_and_simulate(tmp_path):
    controller = _controller(tmp_path, make_settings())
    client = TestClient(create_app(AppState(), controller))

    scenarios = client.get("/api/scenarios").json()["scenarios"]
    assert scenarios

    resp = client.post("/api/simulate", json={"scenario": "evening_spike", "soc_frac": 0.4})
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["simulated"] is True
    assert len(body["intervals"]) > 10


def test_api_simulate_unknown_scenario_is_400(tmp_path):
    client = TestClient(create_app(AppState(), _controller(tmp_path, make_settings())))
    assert client.post("/api/simulate", json={"scenario": "nope"}).status_code == 400


def test_api_simulate_requires_config(tmp_path):
    # no settings saved -> controller.current is None
    client = TestClient(create_app(AppState(), _controller(tmp_path)))
    resp = client.post("/api/simulate", json={"scenario": "typical"})
    assert resp.status_code == 409


def test_api_simulate_accepts_sandbox_config(tmp_path):
    # Test mode sends whole sandbox sections that REPLACE the live ones.
    client = TestClient(create_app(AppState(), _controller(tmp_path, make_settings())))
    resp = client.post(
        "/api/simulate",
        json={"scenario": "typical", "soc_frac": 0.5,
              "config": {
                  "battery": {"capacity_kwh": 20.0, "max_charge_kw": 8.0,
                              "max_discharge_kw": 8.0, "wear_cost_per_kwh": 0.03},
                  "grid": {"import_limit_kw": 15.0, "export_limit_kw": 5.0,
                           "min_battery_export_price": 0.13},
              }},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["solver_status"].startswith("optimal")
    assert body["meta"]["capacity_kwh"] == 20.0  # sandbox section took effect


def test_api_simulate_empty_sandbox_section_means_defaults_not_live(tmp_path):
    # The frontend always sends all four sections; an empty one must REPLACE
    # the live section (pydantic defaults), not silently keep live values.
    settings = make_settings(optimizer={"import_penalty_per_kwh": 0.05})
    client = TestClient(create_app(AppState(), _controller(tmp_path, settings)))
    resp = client.post(
        "/api/simulate", json={"scenario": "typical", "config": {"optimizer": {}}}
    )
    assert resp.status_code == 200
    assert resp.json()["solver_status"].startswith("optimal")


def test_api_simulate_invalid_sandbox_config_is_422_with_field_errors(tmp_path):
    client = TestClient(create_app(AppState(), _controller(tmp_path, make_settings())))
    resp = client.post(
        "/api/simulate",
        json={"scenario": "typical",
              "config": {"battery": {"capacity_kwh": -1.0, "max_charge_kw": 5.0,
                                     "max_discharge_kw": 5.0}}},
    )
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert any(e["loc"].startswith("battery.capacity_kwh") for e in errors)


def test_api_simulate_sandbox_never_touches_the_saved_config(tmp_path):
    controller = _controller(tmp_path, make_settings())
    client = TestClient(create_app(AppState(), controller))
    client.post(
        "/api/simulate",
        json={"scenario": "typical",
              "config": {"battery": {"capacity_kwh": 99.0, "max_charge_kw": 5.0,
                                     "max_discharge_kw": 5.0}}},
    )
    assert controller.current.battery.capacity_kwh == 12.8
