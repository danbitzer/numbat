"""Open-Meteo Solar Forecast adapter.

Parses the `watts` attribute (ISO local timestamp -> instantaneous W at 15-min
resolution) of the energy_production_today/_tomorrow sensors and merges them
into one UTC Series in kW.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from numbat.config import Entities
from numbat.ha.client import HaClient, State
from numbat.models import Series

log = logging.getLogger(__name__)


class SolarParseError(Exception):
    pass


def parse_watts_attribute(state: State) -> dict[datetime, float]:
    raw = state.attributes.get("watts")
    if not isinstance(raw, dict) or not raw:
        raise SolarParseError(
            f"{state.entity_id}: missing/empty 'watts' attribute — is this an "
            "Open-Meteo Solar Forecast energy production sensor?"
        )
    try:
        return {
            datetime.fromisoformat(ts).astimezone(UTC): float(w) / 1000.0
            for ts, w in raw.items()
        }
    except (TypeError, ValueError) as e:
        raise SolarParseError(f"{state.entity_id}: bad watts entry") from e


class OpenMeteoSolarAdapter:
    def __init__(self, client: HaClient, entities: Entities):
        self._client = client
        self._entities = entities

    async def get_pv(self) -> Series:
        today_state, tomorrow_state = await asyncio.gather(
            self._client.get_state(self._entities.pv_forecast_today),
            self._client.get_state(self._entities.pv_forecast_tomorrow),
        )
        points = parse_watts_attribute(today_state)
        try:
            points |= parse_watts_attribute(tomorrow_state)
        except SolarParseError:
            log.warning("tomorrow's PV forecast unavailable; horizon tail will hold today's")
        times = sorted(points)
        return Series(times=times, values=[points[t] for t in times])
