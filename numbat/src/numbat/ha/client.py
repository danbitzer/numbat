"""Async Home Assistant client (REST + WebSocket state watching).

Works identically against the Supervisor proxy (add-on) and a direct HA URL with a
long-lived token (dev) — the difference is entirely in the HaConnection it's given.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import aiohttp

from numbat.config import HaConnection

log = logging.getLogger(__name__)

UNAVAILABLE_STATES = frozenset({"unavailable", "unknown", ""})


class EntityNotFoundError(Exception):
    def __init__(self, entity_id: str):
        super().__init__(f"Entity not found in Home Assistant: {entity_id}")
        self.entity_id = entity_id


@dataclass(frozen=True)
class State:
    entity_id: str
    state: str
    attributes: dict[str, Any]
    last_updated: datetime  # when the VALUE last changed
    # when the integration last reported, even unchanged (HA last_reported);
    # use this for staleness — a battery sitting at 78% for an hour still
    # reports every poll, but last_updated stays frozen.
    last_reported: datetime | None = None

    @property
    def freshness(self) -> datetime:
        return self.last_reported or self.last_updated

    @property
    def available(self) -> bool:
        return self.state not in UNAVAILABLE_STATES

    def as_float(self) -> float:
        return float(self.state)


class HaClient:
    def __init__(self, conn: HaConnection, request_timeout_s: float = 15.0):
        self._conn = conn
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_s)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Self:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._conn.token}"},
            timeout=self._timeout,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("HaClient must be used as an async context manager")
        return self._session

    def _url(self, path: str) -> str:
        return f"{self._conn.rest_url}/{path.lstrip('/')}"

    async def api_ok(self) -> bool:
        try:
            async with self.session.get(self._url("/")) as resp:
                return resp.status == 200
        except aiohttp.ClientError:
            return False

    async def get_state(self, entity_id: str) -> State:
        async with self.session.get(self._url(f"/states/{entity_id}")) as resp:
            if resp.status == 404:
                raise EntityNotFoundError(entity_id)
            resp.raise_for_status()
            data = await resp.json()
        return _parse_state(data)

    async def list_states(self) -> list[State]:
        """Every entity in HA — feeds the Settings view's entity pickers."""
        async with self.session.get(self._url("/states")) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [_parse_state(s) for s in data]

    async def get_states(self, entity_ids: list[str]) -> dict[str, State]:
        """Fetch all states in one call and pick out the requested entities."""
        async with self.session.get(self._url("/states")) as resp:
            resp.raise_for_status()
            data = await resp.json()
        wanted = set(entity_ids)
        found = {s["entity_id"]: _parse_state(s) for s in data if s["entity_id"] in wanted}
        if missing := wanted - found.keys():
            raise EntityNotFoundError(", ".join(sorted(missing)))
        return found

    async def set_state(
        self, entity_id: str, state: str | float, attributes: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {"state": str(state)}
        if attributes:
            payload["attributes"] = attributes
        async with self.session.post(self._url(f"/states/{entity_id}"), json=payload) as resp:
            resp.raise_for_status()

    async def get_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, str]]:
        """Recorder history for one entity as (timestamp, state) tuples.

        Uses minimal_response, so entries between the first and last carry only
        last_changed + state — exactly what a piecewise-constant series needs.
        """
        url = self._url(f"/history/period/{start.isoformat()}")
        params = {
            "filter_entity_id": entity_id,
            "end_time": end.isoformat(),
            "minimal_response": "",
            "no_attributes": "",
        }
        async with self.session.get(url, params=params) as resp:
            if resp.status == 404:
                raise EntityNotFoundError(entity_id)
            resp.raise_for_status()
            data = await resp.json()
        if not data:
            return []
        out = []
        for item in data[0]:
            ts = item.get("last_changed") or item.get("last_updated")
            if ts is None:
                continue
            out.append((datetime.fromisoformat(ts), item["state"]))
        return out

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any], return_response: bool = False
    ) -> Any:
        url = self._url(f"/services/{domain}/{service}")
        if return_response:
            url += "?return_response"
        async with self.session.post(url, json=data) as resp:
            resp.raise_for_status()
            body = await resp.json()
        return body["service_response"] if return_response else body


    async def ws_command(self, message: dict[str, Any], timeout_s: float = 30.0) -> Any:
        """One-shot WebSocket command: connect, auth, send, return the result.

        Some recorder APIs (long-term statistics) exist only over WebSocket.
        A fresh connection per call is fine at daily cadence.
        """
        async with (
            self.session.ws_connect(self._conn.ws_url, heartbeat=30) as ws,
            asyncio.timeout(timeout_s),
        ):
            first = await ws.receive_json()
            if first.get("type") != "auth_required":
                raise RuntimeError(f"unexpected WS greeting: {first.get('type')}")
            await ws.send_json({"type": "auth", "access_token": self._conn.token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"WebSocket auth failed: {auth}")
            await ws.send_json({"id": 1, **message})
            while True:
                reply = await ws.receive_json()
                if reply.get("type") != "result":
                    continue
                if not reply.get("success"):
                    raise RuntimeError(f"WS command failed: {reply.get('error')}")
                return reply.get("result")

    async def get_statistics(
        self,
        statistic_ids: list[str],
        start: datetime,
        end: datetime,
        *,
        units: dict[str, str] | None = None,
    ) -> dict[str, list[tuple[datetime, float]]]:
        """Hourly long-term statistics (mean) as (interval start UTC, value).

        LTS survives recorder purging, so this reaches months back where
        get_history reaches days. Sensors without a state_class have no LTS
        and simply come back absent from the result.
        """
        result = await self.ws_command(
            {
                "type": "recorder/statistics_during_period",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "statistic_ids": statistic_ids,
                "period": "hour",
                "types": ["mean"],
                **({"units": units} if units else {}),
            }
        )
        out: dict[str, list[tuple[datetime, float]]] = {}
        for stat_id, rows in (result or {}).items():
            series = []
            for row in rows:
                if row.get("mean") is None:
                    continue
                ts = row["start"]
                # ms epoch from modern HA; ISO strings from older versions
                when = (
                    datetime.fromtimestamp(ts / 1000, tz=UTC)
                    if isinstance(ts, (int, float))
                    else datetime.fromisoformat(ts)
                )
                series.append((when, float(row["mean"])))
            out[stat_id] = series
        return out

    async def get_statistics_metadata(self, statistic_ids: list[str]) -> dict[str, str | None]:
        """statistic_id -> unit for entities that HAVE long-term statistics;
        entities without a state_class are simply absent from the result."""
        result = await self.ws_command(
            {"type": "recorder/get_statistics_metadata", "statistic_ids": statistic_ids}
        )
        return {
            m["statistic_id"]: m.get("statistics_unit_of_measurement") for m in (result or [])
        }

    async def watch_states(
        self,
        entity_ids: set[str],
        on_change: Callable[[str, str, str | None, dict | None, dict | None], None],
    ) -> None:
        """Subscribe to state_changed events over WebSocket and invoke
        on_change(entity_id, new_state, old_state, new_attrs, old_attrs) for
        the given entities.

        Runs until the connection drops (then raises) — callers wrap this in
        a reconnect loop. The 5-min poll cycle continues regardless, so this
        is a latency optimization, not a correctness requirement.
        """
        async with self.session.ws_connect(self._conn.ws_url, heartbeat=30) as ws:
            # The proxy can accept the socket while core is down and keep the
            # connection alive with protocol pongs — without a handshake
            # timeout the watcher would hang forever, silently deaf.
            async with asyncio.timeout(15):
                first = await ws.receive_json()
                if first.get("type") != "auth_required":
                    raise RuntimeError(f"unexpected WS greeting: {first.get('type')}")
                await ws.send_json({"type": "auth", "access_token": self._conn.token})
                auth = await ws.receive_json()
                if auth.get("type") != "auth_ok":
                    raise RuntimeError(f"WebSocket auth failed: {auth}")
                await ws.send_json(
                    {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
                )
                result = await ws.receive_json()
                if result.get("type") != "result" or not result.get("success"):
                    raise RuntimeError(f"WS subscription rejected: {result}")
            log.info("watching %d entities for state changes", len(entity_ids))
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                payload = msg.json()
                if payload.get("type") != "event":
                    continue
                data = payload.get("event", {}).get("data", {})
                entity_id = data.get("entity_id")
                new = data.get("new_state") or {}
                old = data.get("old_state") or {}
                if entity_id in entity_ids and new.get("state") is not None:
                    on_change(
                        entity_id,
                        new["state"],
                        old.get("state"),
                        new.get("attributes"),
                        old.get("attributes"),
                    )
        raise ConnectionError("WebSocket connection closed")


def _parse_state(data: dict[str, Any]) -> State:
    last_reported = data.get("last_reported")
    return State(
        entity_id=data["entity_id"],
        state=data["state"],
        attributes=data.get("attributes", {}),
        last_updated=datetime.fromisoformat(data["last_updated"]),
        last_reported=datetime.fromisoformat(last_reported) if last_reported else None,
    )
