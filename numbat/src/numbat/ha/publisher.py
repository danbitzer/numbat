"""Publish Numbat output sensors via POST /api/states.

REST-created entities are ephemeral (gone on HA restart, no unique_id), so every
sensor is republished unconditionally each cycle.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from numbat import __version__
from numbat.ha.client import HaClient
from numbat.models import Plan

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, client: HaClient):
        self._client = client

    async def publish_status(
        self,
        status: str,
        *,
        last_solve: datetime | None = None,
        solve_ms: float | None = None,
        detail: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        attrs: dict[str, Any] = {
            "friendly_name": "Numbat status",
            "icon": "mdi:heart-pulse",
            "version": __version__,
            "heartbeat": datetime.now(UTC).isoformat(),
        }
        if last_solve is not None:
            attrs["last_solve"] = last_solve.isoformat()
        if solve_ms is not None:
            attrs["solve_ms"] = round(solve_ms, 1)
        if detail:
            attrs["detail"] = detail
        if extra:
            attrs.update(extra)
        await self._client.set_state("sensor.numbat_status", status, attrs)

    async def publish_vacation(self, vacation: dict | None) -> None:
        """binary_sensor.numbat_vacation_mode — HA-side visibility only (the
        actuator deliberately does NOT read this; while on vacation Numbat's own
        plans already reflect the flat baseline)."""
        attrs: dict[str, Any] = {
            "friendly_name": "Numbat vacation mode",
            "icon": "mdi:palm-tree",
        }
        if vacation:
            attrs["baseline_kw"] = vacation.get("baseline_kw")
            attrs["until"] = vacation.get("until")
        await self._client.set_state(
            "binary_sensor.numbat_vacation_mode", "on" if vacation else "off", attrs
        )

    # Published until 0.22: the battery power as its own sensor, ~50 ms before
    # the action sensor. Actuators triggering on it read the action sensor's
    # PREVIOUS power (live 2026-10-09) — the action sensor has carried
    # power_kw/power_w atomically since 0.18, so the setpoint sensor was a
    # second source of truth with a built-in race. Removed on startup so a
    # stale copy can't linger until the next HA restart.
    LEGACY_SENSORS = ("sensor.numbat_power_setpoint",)

    async def retire_legacy_sensors(self) -> None:
        for entity_id in self.LEGACY_SENSORS:
            try:
                if await self._client.delete_state(entity_id):
                    log.info("removed legacy entity %s", entity_id)
            except Exception as e:  # noqa: BLE001 - best effort, never blocks startup
                log.warning("could not remove legacy entity %s: %s", entity_id, e)

    async def publish_plan(self, plan: Plan, capacity_kwh: float) -> None:
        """Publish the full dry-run sensor set (republished every cycle).

        The action sensor is the single source of truth for actuators: the
        action and its power (power_kw / power_w) go out in ONE state post,
        so an automation can never pair a fresh action with a stale power or
        vice versa.
        """
        step0 = plan.intervals[0]
        await self._client.set_state(
            "sensor.numbat_action",
            step0.action.value,
            {
                "friendly_name": "Numbat recommended action",
                "icon": "mdi:battery-charging",
                "solver_status": plan.solver_status,
                "valid_until": step0.end.isoformat(),
                "live_spike": plan.live_spike,
                # export withheld this interval (can be true DURING charge —
                # negative buy and feed-in); actuators cap the export limit
                # on this, not on action == "curtail"
                "curtail": plan.curtail_export,
                # PV generation withheld this interval (negative buy: the
                # house/charge should draw from the grid, which pays);
                # actuators that can stop PV key off this, atomic with the
                # action it rides (hold or charge)
                "pv_off": plan.pv_off,
                # the flow vocabulary (numbat.flow): what the energy is doing
                # this interval in household words, derived action-first from
                # the plan, and the solar it throws away — additive, for
                # dashboards/template sensors; automations keep keying on the
                # state and the two flags above
                "flow": step0.flow,
                "pv_spill_kw": step0.pv_spill_kw,
                # the battery power rides the action in ONE atomic POST — the
                # only place actuators read it, so action and magnitude can
                # never disagree
                "power_kw": round(step0.power_kw, 3),
                "power_w": round(abs(step0.power_kw) * 1000),
            },
        )
        await self._client.set_state(
            "sensor.numbat_soc_target",
            round(100 * step0.soc_end / capacity_kwh, 1),
            {
                "friendly_name": "Numbat SoC target (end of interval)",
                "unit_of_measurement": "%",
                "icon": "mdi:battery-70",
            },
        )
        await self._client.set_state(
            "sensor.numbat_horizon_cost",
            round(plan.objective_cost, 2),
            {
                "friendly_name": "Numbat expected horizon cost",
                "unit_of_measurement": "$",
                "icon": "mdi:cash-multiple",
                "horizon_end": plan.intervals[-1].end.isoformat(),
            },
        )
