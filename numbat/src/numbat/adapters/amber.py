"""Amber Electric price adapter (Amber Express HACS integration only).

AmberExpressAdapter parses the `forecast` attribute of the Amber Express
price sensors — a list of {time, value} entries in $/kWh where the values are
Amber's advanced price prediction and the first entry is the current
interval. On a 5-minute site entries are 5-min near-term then 30-min.

Sign conventions (locked to tests/fixtures/amber_express_feed_in_price.yaml):
feed-in arrives positive = export revenue, matching Numbat's convention — no
flip. Negative feed-in (paying to export) passes through as negative.

The HA core `amberelectric` integration is deliberately NOT supported (its
`forecasts` attribute has 2dp price resolution and no advanced-price mode);
Amber Express is free, strictly better for optimization, and what Numbat tests
against.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from numbat.config import Entities
from numbat.ha.client import HaClient, State
from numbat.models import PriceForecast, Series

log = logging.getLogger(__name__)

SPIKE_ACTIVE_STATES = frozenset({"on"})


class PriceProvider(Protocol):
    async def get_prices(self) -> PriceForecast: ...


class PriceParseError(Exception):
    pass


def parse_forecast_attribute(state: State) -> Series:
    """Parse an Amber Express `forecast` attribute into a UTC Series."""
    raw = state.attributes.get("forecast")
    if not isinstance(raw, list) or not raw:
        raise PriceParseError(
            f"{state.entity_id}: missing/empty 'forecast' attribute — is this an "
            "Amber Express price sensor?"
        )
    times: list[datetime] = []
    values: list[float] = []
    for entry in raw:
        try:
            times.append(datetime.fromisoformat(entry["time"]).astimezone(UTC))
            values.append(float(entry["value"]))
        except (KeyError, TypeError, ValueError) as e:
            raise PriceParseError(f"{state.entity_id}: bad forecast entry {entry!r}") from e
    unit = state.attributes.get("unit_of_measurement")
    if unit and unit != "$/kWh":
        raise PriceParseError(f"{state.entity_id}: expected $/kWh, got {unit!r}")
    return Series(times=times, values=values)


def _spike_active(spike_state: State | None) -> bool:
    if spike_state is None or not spike_state.available:
        return False
    if spike_state.state in SPIKE_ACTIVE_STATES:
        return True
    return spike_state.attributes.get("spike_status") == "spike"


class AmberExpressAdapter:
    def __init__(self, client: HaClient, entities: Entities):
        self._client = client
        self._entities = entities

    async def get_prices(self) -> PriceForecast:
        spike_task = (
            self._client.get_state(self._entities.price_spike)
            if self._entities.price_spike
            else _none()
        )
        buy_state, sell_state, spike_state = await asyncio.gather(
            self._client.get_state(self._entities.buy_price),
            self._client.get_state(self._entities.sell_price),
            spike_task,
        )
        for s in (buy_state, sell_state):
            if not s.available:
                raise PriceParseError(f"{s.entity_id} is {s.state}")

        buy = parse_forecast_attribute(buy_state)
        sell = parse_forecast_attribute(sell_state)
        return PriceForecast(
            buy=buy,
            sell=sell,
            current_buy=buy_state.as_float(),
            current_sell=sell_state.as_float(),
            live_spike=_spike_active(spike_state),
            updated_at=min(buy_state.freshness, sell_state.freshness),
            current_estimate=bool(buy_state.attributes.get("estimate"))
            or bool(sell_state.attributes.get("estimate")),
        )


async def _none() -> Any:
    return None
