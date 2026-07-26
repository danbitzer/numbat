from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer

from numbat.config import HaConnection
from numbat.ha.client import HaClient, State

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture_state(name: str) -> State:
    """Load a captured entity state (tests/fixtures/<name>.yaml) as a State."""
    data = yaml.safe_load((FIXTURES / f"{name}.yaml").read_text())
    return State(
        entity_id=data["entity_id"],
        state=str(data["state"]),
        attributes=data["attributes"],
        last_updated=datetime.fromisoformat(data["last_updated"]),
    )


def fixture_state_payload(name: str) -> dict:
    """Same fixture as a raw /api/states response dict (for FakeHa)."""
    data = yaml.safe_load((FIXTURES / f"{name}.yaml").read_text())
    return {
        "entity_id": data["entity_id"],
        "state": str(data["state"]),
        "attributes": data["attributes"],
        "last_updated": data["last_updated"],
    }


class FakeHa:
    """Minimal /api/states surface with request capture."""

    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.posted: list[tuple[str, dict]] = []
        self.service_calls: list[tuple[str, str, dict]] = []
        self.service_responses: dict[tuple[str, str], dict] = {}
        # fault injection: entity_id -> HTTP status for GET /states/<id>
        self.state_errors: dict[str, int] = {}
        # fault injection: called with (domain, service, data), return an HTTP
        # status to fail that call, or None to succeed
        self.service_fault: object = None
        self.app = web.Application()
        self.app.router.add_get("/api/", self._root)
        # recorder history: entity_id -> list of raw history item dicts
        self.history: dict[str, list[dict]] = {}
        self.history_requests: list[dict] = []
        # long-term statistics: statistic_id -> list of raw stat row dicts;
        # metadata: statistic_id -> unit (only ids present here "have" LTS)
        self.statistics: dict[str, list[dict]] = {}
        self.statistics_meta: dict[str, str | None] = {}
        self.ws_commands: list[dict] = []
        self.app.router.add_get("/api/states/{entity_id}", self._get_state)
        self.app.router.add_post("/api/states/{entity_id}", self._post_state)
        self.app.router.add_post("/api/services/{domain}/{service}", self._call_service)
        self.app.router.add_get("/api/history/period/{start}", self._get_history)
        self.app.router.add_get("/api/websocket", self._websocket)

    def add_fixture(self, name: str) -> str:
        payload = fixture_state_payload(name)
        self.states[payload["entity_id"]] = payload
        return payload["entity_id"]

    async def _root(self, request: web.Request) -> web.Response:
        return web.json_response({"message": "API running."})

    async def _get_state(self, request: web.Request) -> web.Response:
        entity_id = request.match_info["entity_id"]
        if entity_id in self.state_errors:
            return web.json_response({"message": "injected"}, status=self.state_errors[entity_id])
        if entity_id not in self.states:
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response(self.states[entity_id])

    async def _post_state(self, request: web.Request) -> web.Response:
        entity_id = request.match_info["entity_id"]
        self.posted.append((entity_id, await request.json()))
        return web.json_response({}, status=201)

    async def _get_history(self, request: web.Request) -> web.Response:
        entity_id = request.query.get("filter_entity_id", "")
        self.history_requests.append(
            {"start": request.match_info["start"], **dict(request.query)}
        )
        if entity_id not in self.history:
            return web.json_response([])
        return web.json_response([self.history[entity_id]])

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != "tok":
            await ws.send_json({"type": "auth_invalid"})
            return ws
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                break
            req = msg.json()
            self.ws_commands.append(req)
            reply = {"id": req.get("id"), "type": "result", "success": True}
            if req.get("type") == "recorder/statistics_during_period":
                reply["result"] = {
                    sid: rows
                    for sid, rows in self.statistics.items()
                    if sid in req["statistic_ids"]
                }
            elif req.get("type") == "recorder/get_statistics_metadata":
                reply["result"] = [
                    {"statistic_id": sid, "statistics_unit_of_measurement": unit}
                    for sid, unit in self.statistics_meta.items()
                    if sid in req["statistic_ids"]
                ]
            else:
                reply.update(success=False, error={"message": "unknown command"})
            await ws.send_json(reply)
        return ws

    async def _call_service(self, request: web.Request) -> web.Response:
        domain, service = request.match_info["domain"], request.match_info["service"]
        data = await request.json()
        self.service_calls.append((domain, service, data))
        if self.service_fault:
            status = self.service_fault(domain, service, data)  # type: ignore[operator]
            if status:
                return web.json_response({"message": "injected"}, status=status)
        if "return_response" in request.query_string:
            response = self.service_responses.get((domain, service), {})
            return web.json_response({"changed_states": [], "service_response": response})
        return web.json_response([])


@asynccontextmanager
async def fake_ha_client(fake: FakeHa) -> AsyncIterator[HaClient]:
    server = TestServer(fake.app)
    await server.start_server()
    try:
        base = str(server.make_url("")).rstrip("/")
        ws_url = f"ws://{base.split('://', 1)[1]}/api/websocket"
        conn = HaConnection(rest_url=f"{base}/api", ws_url=ws_url, token="tok")
        async with HaClient(conn) as client:
            yield client
    finally:
        await server.close()
