import json
from pathlib import Path

import pytest

from numbat.config import EnvSettings, Settings, resolve_connection, resolve_log_level
from numbat.config_store import ConfigStore

MINIMAL_CONFIG = {
    "entities": {
        "buy_price": "sensor.amber_express_home_general_price",
        "sell_price": "sensor.amber_express_home_feed_in_price",
        "pv_forecast_today": "sensor.energy_production_today",
        "pv_forecast_tomorrow": "sensor.energy_production_tomorrow",
        "battery_soc": "sensor.battery_level",
        "battery_power": "sensor.battery_power",
        "weather": "weather.henley_beach_hourly",
    },
    "battery": {"capacity_kwh": 12.8, "max_charge_kw": 5.0, "max_discharge_kw": 5.0},
    "grid": {"import_limit_kw": 15.0, "export_limit_kw": 5.0},
}


def make_settings(**overrides) -> Settings:
    return Settings.model_validate({**MINIMAL_CONFIG, **overrides})


def test_minimal_config_defaults():
    settings = make_settings()
    assert settings.optimizer.horizon_hours == 36
    # 5%: at (or just above) a typical inverter's enforced minimum — soc_min
    # floors ALL planned discharge, so a high value defends itself with
    # imports (see DOCS); hungry-day insurance belongs to load.buffer
    assert settings.battery.soc_min == 0.05
    # 3c = top of the honest lithium wear range the field help quotes
    assert settings.battery.wear_cost_per_kwh == 0.03
    # haircut defaults OFF (trust the forecast until the user opts into
    # skepticism — the help suggests 5-10% for optimistic feeds)
    assert settings.optimizer.forecast_haircut == 0.0
    # Numbat starts disabled: the enable toggle in the UI is a deliberate act
    assert settings.enabled is False
    # no load sensor is a valid (degraded) config — Numbat plans with zero load
    assert settings.entities.load_power == ""


def test_weather_entity_is_optional():
    # without a weather entity the temp response is simply disabled
    config = json.loads(json.dumps(MINIMAL_CONFIG))
    del config["entities"]["weather"]
    settings = Settings.model_validate(config)
    assert settings.entities.weather == ""


def test_redesign_defaults_and_overrides():
    """The optimizer-redesign knobs: hold-value anchor, export margin, windowed
    daily target, manual export floor."""
    s = make_settings()
    assert s.optimizer.hold_value_floor == 0.01
    assert s.optimizer.hold_value_scaling == 1.0
    # 5c defaults: a fresh install shouldn't churn the battery for
    # sub-wear-cost export margins or import on thin resale bets
    assert s.optimizer.min_battery_export_spread == 0.05
    assert s.optimizer.import_penalty_per_kwh == 0.05
    assert s.battery.daily_target_hold_hours == 4.0
    # 5c/kWh·h × the 4 h hold still values a full-window missing kWh at 20c
    assert s.battery.daily_target_penalty_per_kwh == 0.05
    assert s.battery.daily_target_penalty_price_multiple == 0.0
    assert s.grid.min_battery_export_price is None

    s = make_settings(
        optimizer={"hold_value_scaling": 1.2, "min_battery_export_spread": 0.02},
        grid={"import_limit_kw": 15.0, "export_limit_kw": 5.0, "min_battery_export_price": 0.11},
        battery={
            "capacity_kwh": 44.8, "max_charge_kw": 12.0, "max_discharge_kw": 12.0,
            "daily_target_hold_hours": 6.0, "daily_target_penalty_price_multiple": 3.0,
        },
    )
    assert s.optimizer.min_battery_export_spread == 0.02
    assert s.grid.min_battery_export_price == 0.11
    assert s.battery.daily_target_hold_hours == 6.0
    assert s.battery.daily_target_penalty_price_multiple == 3.0


def test_negative_redesign_values_rejected():
    for section, field in (
        ("optimizer", "hold_value_floor"),
        ("optimizer", "min_battery_export_spread"),
        ("battery", "daily_target_hold_hours"),
    ):
        base = {
            "battery": {"capacity_kwh": 12.8, "max_charge_kw": 5.0, "max_discharge_kw": 5.0},
        }
        base.setdefault(section, {})[field] = -0.5
        with pytest.raises(ValueError):
            make_settings(**base)


def test_invalid_soc_bounds_rejected():
    config = json.loads(json.dumps(MINIMAL_CONFIG))
    config["battery"]["soc_min"] = 0.9
    config["battery"]["soc_max"] = 0.5
    with pytest.raises(ValueError, match="soc_min"):
        Settings.model_validate(config)


def test_spike_reserve_defaults_and_bounds():
    s = make_settings()
    # manual insurance: off by default, $1 release threshold
    assert s.spike.reserve_soc == 0.0
    assert s.spike.high_price_threshold == 1.0
    # dropped pre-0.15 spike keys are ignored, not fatal
    config = json.loads(json.dumps(MINIMAL_CONFIG))
    config["spike"] = {"reserve_soc": 0.3, "lookahead_hours": 4, "reserve_kwh": 6.0,
                      "reserve_penalty_per_kwh": 0.5}
    assert Settings.model_validate(config).spike.reserve_soc == 0.3
    # a reserve above usable capacity is rejected
    config["battery"]["soc_max"] = 0.9
    config["spike"]["reserve_soc"] = 0.95
    with pytest.raises(ValueError, match="spike.reserve_soc"):
        Settings.model_validate(config)


def test_export_reserve_default_and_bounded_by_soc_max():
    # 10% over the 5% soc_min default: the sell floor is active out of the
    # box (the gate binds only while reserve > soc_min). Explicit 0 = off.
    assert make_settings().battery.export_reserve_soc == 0.10
    config = json.loads(json.dumps(MINIMAL_CONFIG))
    config["battery"]["soc_max"] = 0.9
    config["battery"]["export_reserve_soc"] = 0.95
    with pytest.raises(ValueError, match="export_reserve_soc"):
        Settings.model_validate(config)


def test_store_roundtrip_and_backup(tmp_path: Path):
    store = ConfigStore(tmp_path / "numbat-config.json")
    assert store.load() is None  # unconfigured

    store.save(make_settings(enabled=True))
    doc = json.loads(store.path.read_text())
    assert doc["schema_version"] == 1
    loaded = store.load()
    assert loaded is not None and loaded.enabled is True

    store.save(make_settings(enabled=False))
    assert store.load().enabled is False
    # previous version preserved
    bak = json.loads((tmp_path / "numbat-config.json.bak").read_text())
    assert bak["config"]["enabled"] is True


def test_store_corrupt_file_is_unconfigured_not_fatal(tmp_path: Path):
    path = tmp_path / "numbat-config.json"
    for content in (
        "{not json",
        json.dumps({"schema_version": 1, "config": {"battery": {}}}),  # invalid settings
        json.dumps([1, 2, 3]),  # valid JSON, not an object
        json.dumps("hello"),
    ):
        path.write_text(content)
        assert ConfigStore(path).load() is None


def test_store_save_never_leaves_the_live_path_absent(tmp_path: Path):
    # .bak is a hardlink of the previous version, not a rename — a crash
    # mid-save must still leave numbat-config.json in place
    store = ConfigStore(tmp_path / "numbat-config.json")
    store.save(make_settings(enabled=True))
    store.save(make_settings(enabled=False))
    assert store.path.exists()
    bak = json.loads((tmp_path / "numbat-config.json.bak").read_text())
    assert bak["config"]["enabled"] is True
    assert store.load().enabled is False


def test_unknown_keys_ignored():
    # a stale key from an older config version is ignored, not fatal
    config = json.loads(json.dumps(MINIMAL_CONFIG))
    config["entities"]["buy_forecast"] = "sensor.amber_general_forecast"
    Settings.model_validate(config)


def env_settings(**kwargs) -> EnvSettings:
    return EnvSettings(_env_file=None, **kwargs)  # hermetic: ignore any real .env


def test_log_level_env_wins():
    assert resolve_log_level(env_settings(log_level="debug")) == "debug"


def test_log_level_defaults_to_info_without_options_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # hermetic: never read a real /data/options.json on the machine running tests
    monkeypatch.setattr("numbat.config.DEFAULT_OPTIONS_FILE", str(tmp_path / "options.json"))
    assert resolve_log_level(env_settings()) == "info"


def test_log_level_from_supervisor_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"log_level": "warning"}))
    monkeypatch.setattr("numbat.config.DEFAULT_OPTIONS_FILE", str(opts))
    assert resolve_log_level(env_settings()) == "warning"


def test_connection_supervisor():
    conn = resolve_connection(env_settings(), supervisor_token="tok")
    assert conn.rest_url == "http://supervisor/core/api"
    assert conn.ws_url == "ws://supervisor/core/websocket"
    assert conn.token == "tok"


def test_connection_standalone():
    env = env_settings(ha_url="http://homeassistant.local:8123/", ha_token="tok")
    conn = resolve_connection(env, supervisor_token="")
    assert conn.rest_url == "http://homeassistant.local:8123/api"
    assert conn.ws_url == "ws://homeassistant.local:8123/api/websocket"


def test_connection_standalone_https():
    env = env_settings(ha_url="https://ha.example.com", ha_token="tok")
    conn = resolve_connection(env, supervisor_token="")
    assert conn.ws_url == "wss://ha.example.com/api/websocket"


def test_connection_unconfigured():
    with pytest.raises(RuntimeError, match="NUMBAT_HA_URL"):
        resolve_connection(env_settings(), supervisor_token="")


def test_env_settings_reads_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / ".env").write_text(
        "NUMBAT_HA_URL=http://ha.local:8123\nNUMBAT_HA_TOKEN=tok\nNUMBAT_CONFIG_FILE=./numbat-config.json\n"
    )
    monkeypatch.chdir(tmp_path)
    env = EnvSettings()
    assert env.ha_url == "http://ha.local:8123"
    assert env.config_file == Path("./numbat-config.json")
