from datetime import UTC, datetime, timedelta

from freezegun import freeze_time

from numbat.config import Settings
from numbat.main import PriceWatcher, seconds_to_next_boundary
from numbat.web.app import HealthState


def test_boundary_mid_interval():
    # 09:02:30 → 150s to 09:05:00
    epoch = datetime(2026, 7, 15, 9, 2, 30, tzinfo=UTC).timestamp()
    assert seconds_to_next_boundary(epoch) == 150.0


def test_boundary_exactly_on_tick_waits_full_period():
    epoch = datetime(2026, 7, 15, 9, 5, 0, tzinfo=UTC).timestamp()
    assert seconds_to_next_boundary(epoch) == 300.0


def test_boundary_never_returns_less_than_1s():
    epoch = datetime(2026, 7, 15, 9, 4, 59, 500000, tzinfo=UTC).timestamp()
    assert seconds_to_next_boundary(epoch) >= 1.0


def make_watcher() -> PriceWatcher:
    settings = Settings.model_validate(
        {
            "entities": {
                "buy_price": "sensor.buy",
                "sell_price": "sensor.sell",
                "price_spike": "binary_sensor.spike",
                "pv_forecast_today": "sensor.pv1",
                "pv_forecast_tomorrow": "sensor.pv2",
                "battery_soc": "sensor.soc",
                "battery_power": "sensor.power",
                "weather": "weather.home",
            },
            "battery": {"capacity_kwh": 10, "max_charge_kw": 5, "max_discharge_kw": 5},
            "grid": {"import_limit_kw": 15, "export_limit_kw": 5},
        }
    )
    return PriceWatcher(settings)


def test_watcher_first_change_after_restart_triggers():
    # the very first observed event carries old_state — a spike confirming
    # minutes after an add-on restart must trigger immediately
    w = make_watcher()
    w.on_change("binary_sensor.spike", "on", "off")
    assert w.trigger.is_set()


def test_watcher_any_price_move_triggers():
    # every solve should reflect the live price — no significance threshold
    w = make_watcher()
    w.on_change("sensor.buy", "0.44", None)  # no old_state: nothing to compare
    assert not w.trigger.is_set()
    w.on_change("sensor.buy", "0.45", "0.44")  # 1c move triggers
    assert w.trigger.is_set()


def test_watcher_estimate_flip_at_same_value_triggers():
    # estimate -> confirmed at the SAME price must re-solve so the
    # dashboard's "unconfirmed" marker clears
    w = make_watcher()
    w.on_change("sensor.buy", "0.44", "0.44", {"estimate": True}, {"estimate": True})
    assert not w.trigger.is_set()  # seeded, no change
    w.on_change("sensor.buy", "0.44", "0.44", {"estimate": False}, {"estimate": True})
    assert w.trigger.is_set()


def test_watcher_spike_status_flip_triggers_without_state_change():
    # _spike_active treats spike_status == "spike" as live even while the
    # binary sensor state is still "off" — that flip alone must re-solve
    w = make_watcher()
    w.on_change(
        "binary_sensor.spike", "off", "off", {"spike_status": "none"}, {"spike_status": "none"}
    )
    assert not w.trigger.is_set()
    w.on_change(
        "binary_sensor.spike", "off", "off", {"spike_status": "spike"}, {"spike_status": "none"}
    )
    assert w.trigger.is_set()


def test_watcher_attribute_noise_does_not_trigger():
    # the forecast list refreshes every poll; unchanged value + estimate flag
    # must not cause a re-solve
    w = make_watcher()
    w.on_change(
        "sensor.buy",
        "0.44",
        "0.44",
        {"estimate": False, "forecast": [1]},
        {"estimate": False, "forecast": [0]},
    )
    w.on_change(
        "sensor.buy",
        "0.44",
        "0.44",
        {"estimate": False, "forecast": [2]},
        {"estimate": False, "forecast": [1]},
    )
    assert not w.trigger.is_set()


def test_health_grace_period_then_degraded():
    with freeze_time("2026-07-15 09:00:00", tz_offset=0) as frozen:
        health = HealthState()
        assert health.healthy  # startup grace
        frozen.tick(timedelta(minutes=16))
        assert not health.healthy  # no cycle ever succeeded
        health.mark_success()
        assert health.healthy
        frozen.tick(timedelta(minutes=14))
        assert health.healthy
        frozen.tick(timedelta(minutes=2))
        assert not health.healthy
