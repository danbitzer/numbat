"""Battery state via HA sensors (Sungrow SHx / mkaiser Modbus package or any
integration exposing SoC + battery power sensors).

Normalization:
- SoC: % -> fraction (unit-of-measurement '%' or value > 1.5 treated as %)
- power: unit auto-detected from unit_of_measurement (W or kW); sign normalized
  to Numbat's convention (positive = charging) via battery.power_convention since
  installs differ.
"""

from __future__ import annotations

import asyncio
import logging

from numbat.config import Battery, Entities
from numbat.ha.client import HaClient, State
from numbat.models import BatteryState

log = logging.getLogger(__name__)


class BatteryParseError(Exception):
    pass


def parse_soc_fraction(state: State) -> float:
    value = state.as_float()
    unit = state.attributes.get("unit_of_measurement") or ""
    if unit == "%":
        value /= 100.0
    elif not unit:
        if value > 1.5:
            value /= 100.0  # unambiguously a percentage
        else:
            # 0..1.5 without a unit: fraction 1.0 or a nearly-empty battery in
            # %? Guessing wrong means discharging an empty battery — refuse.
            raise BatteryParseError(
                f"{state.entity_id}: SoC {value} without unit_of_measurement is "
                "ambiguous (fraction or %?) — use a sensor that declares '%'"
            )
    else:
        raise BatteryParseError(f"{state.entity_id}: unexpected SoC unit {unit!r}")
    if not 0.0 <= value <= 1.0:
        raise BatteryParseError(f"{state.entity_id}: SoC {value} outside [0, 1]")
    return value


def parse_power_kw(state: State, charge_positive: bool) -> float:
    value = state.as_float()
    unit = (state.attributes.get("unit_of_measurement") or "").lower()
    if unit == "w":
        value /= 1000.0
    elif unit != "kw":
        # No silent guessing: a W sensor read as kW is off by 1000x.
        raise BatteryParseError(
            f"{state.entity_id}: power unit {unit!r} not recognised — the sensor "
            "must declare W or kW"
        )
    return value if charge_positive else -value


class SungrowAdapter:
    def __init__(self, client: HaClient, entities: Entities, battery: Battery):
        self._client = client
        self._entities = entities
        self._battery = battery

    async def get_battery_state(self) -> BatteryState:
        soc_state, power_state = await asyncio.gather(
            self._client.get_state(self._entities.battery_soc),
            self._client.get_state(self._entities.battery_power),
        )
        for s in (soc_state, power_state):
            if not s.available:
                raise BatteryParseError(f"{s.entity_id} is {s.state}")
        return BatteryState(
            soc_frac=parse_soc_fraction(soc_state),
            power_kw=parse_power_kw(
                power_state, self._battery.power_convention == "charge_positive"
            ),
            capacity_kwh=self._battery.capacity_kwh,
            ts=min(soc_state.freshness, power_state.freshness),
        )
