"""Temperature forecast via any HA weather entity.

Calls the weather.get_forecasts service (hourly) and extracts temperature
into a UTC Series in °C, for the load forecaster's temp rules.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from numbat.config import Entities
from numbat.ha.client import HaClient
from numbat.models import Series

log = logging.getLogger(__name__)


class WeatherParseError(Exception):
    pass


class WeatherAdapter:
    def __init__(self, client: HaClient, entities: Entities):
        self._client = client
        self._entity_id = entities.weather

    async def get_temperature_forecast(self) -> Series:
        response = await self._client.call_service(
            "weather",
            "get_forecasts",
            {"entity_id": self._entity_id, "type": "hourly"},
            return_response=True,
        )
        try:
            forecast = response[self._entity_id]["forecast"]
        except (KeyError, TypeError) as e:
            raise WeatherParseError(
                f"weather.get_forecasts returned no forecast for {self._entity_id}"
            ) from e
        if not forecast:
            raise WeatherParseError(f"{self._entity_id}: empty hourly forecast")
        times: list[datetime] = []
        values: list[float] = []
        for entry in forecast:
            try:
                times.append(datetime.fromisoformat(entry["datetime"]).astimezone(UTC))
                values.append(float(entry["temperature"]))
            except (KeyError, TypeError, ValueError) as e:
                raise WeatherParseError(f"{self._entity_id}: bad forecast entry {entry!r}") from e
        return Series(times=times, values=values)
