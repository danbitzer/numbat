"""HaClient/Publisher tests against a real in-process aiohttp server."""

import pytest
from conftest import FakeHa, fake_ha_client

from numbat.ha.client import EntityNotFoundError
from numbat.ha.publisher import Publisher

SOC_STATE = {
    "entity_id": "sensor.battery_level",
    "state": "72.5",
    "attributes": {"unit_of_measurement": "%"},
    "last_updated": "2026-07-15T09:00:00.000000+00:00",
}


async def test_get_state():
    fake = FakeHa()
    fake.states["sensor.battery_level"] = SOC_STATE
    async with fake_ha_client(fake) as client:
        assert await client.api_ok()
        state = await client.get_state("sensor.battery_level")
    assert state.as_float() == 72.5
    assert state.available
    assert state.last_updated.tzinfo is not None


async def test_unavailable_state():
    fake = FakeHa()
    fake.states["sensor.battery_level"] = dict(SOC_STATE, state="unavailable")
    async with fake_ha_client(fake) as client:
        state = await client.get_state("sensor.battery_level")
    assert not state.available


async def test_missing_entity_raises():
    fake = FakeHa()
    async with fake_ha_client(fake) as client:
        with pytest.raises(EntityNotFoundError, match="sensor.nope"):
            await client.get_state("sensor.nope")


async def test_publish_status_posts_state():
    fake = FakeHa()
    async with fake_ha_client(fake) as client:
        await Publisher(client).publish_status("ok", detail="test")
    entity_id, body = fake.posted[0]
    assert entity_id == "sensor.numbat_status"
    assert body["state"] == "ok"
    assert body["attributes"]["detail"] == "test"
    assert "heartbeat" in body["attributes"]


async def test_publish_plan_action_carries_curtail_flag():
    """The actuator caps export on the `curtail` attribute (atomic with the
    action) — it must ride the action sensor, not a separate publish."""
    from datetime import UTC, datetime

    from test_planner import make_settings, offline_planner, synthetic_cycle_data

    settings = make_settings()
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings)
    data.prices.current_sell = -0.12
    data.inputs.buy[:] = -0.02
    data.inputs.sell[:] = -0.12
    plan = planner.optimize(data, datetime(2026, 7, 15, 11, 36, 30, tzinfo=UTC))
    assert plan.curtail_export is True

    fake = FakeHa()
    async with fake_ha_client(fake) as client:
        await Publisher(client).publish_plan(plan, capacity_kwh=12.8)
    action_posts = [b for e, b in fake.posted if e == "sensor.numbat_action"]
    assert action_posts and action_posts[0]["attributes"]["curtail"] is True
    assert action_posts[0]["state"] == "charge"
