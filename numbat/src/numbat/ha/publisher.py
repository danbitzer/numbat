"""Publish Numbat output sensors via POST /api/states.

REST-created entities are ephemeral (gone on HA restart, no unique_id), so every
sensor is republished unconditionally each cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from numbat import __version__
from numbat.ha.client import HaClient
from numbat.models import Plan


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

    async def publish_plan(self, plan: Plan, capacity_kwh: float) -> None:
        """Publish the full dry-run sensor set (republished every cycle).

        Setpoint goes out BEFORE action: actuator automations trigger on the
        action change and read the setpoint, so this order means a failure
        between the two leaves the old action with a new setpoint (harmless —
        no trigger fired) rather than a new action driving the previous
        cycle's power.
        """
        step0 = plan.intervals[0]
        await self._client.set_state(
            "sensor.numbat_power_setpoint",
            round(step0.power_kw, 3),
            {
                "friendly_name": "Numbat battery power setpoint",
                "unit_of_measurement": "kW",
                "device_class": "power",
                "icon": "mdi:battery-arrow-up-down",
                "convention": "positive = charging",
                # magnitude in W, for inverter number entities
                "power_w": round(abs(step0.power_kw) * 1000),
            },
        )
        await self._client.set_state(
            "sensor.numbat_action",
            step0.action.value,
            {
                "friendly_name": "Numbat recommended action",
                "icon": "mdi:battery-charging",
                "solver_status": plan.solver_status,
                "valid_until": step0.end.isoformat(),
                "live_spike": plan.live_spike,
                # power duplicated here so action + magnitude change in ONE
                # atomic POST — actuator automations read these, never pairing
                # a fresh action with the previous cycle's setpoint
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
