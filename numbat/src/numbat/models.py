"""Shared domain types.

Internal conventions (adapters normalize to these, nothing else re-converts):
- prices in $/kWh; feed-in positive = revenue
- battery power positive = charging
- all timestamps tz-aware UTC; Series times are interval starts
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True)
class Series:
    """Piecewise-constant timestamped series at source-native resolution.

    values[i] holds from times[i] until times[i+1] ("previous" interpolation).
    Intervals need not be uniform — Amber Express on a 5-minute site emits
    5-min entries near-term and 30-min entries beyond.
    """

    times: list[datetime]  # interval starts, ascending, tz-aware UTC
    values: list[float]

    def __post_init__(self) -> None:
        if not self.times:
            raise ValueError("Series must have at least one point")
        if len(self.times) != len(self.values):
            raise ValueError(f"times ({len(self.times)}) != values ({len(self.values)})")
        if any(b <= a for a, b in zip(self.times, self.times[1:], strict=False)):
            raise ValueError("times must be strictly ascending")

    @property
    def start(self) -> datetime:
        return self.times[0]

    @property
    def end(self) -> datetime:
        return self.times[-1]


@dataclass
class PriceForecast:
    """Prices in $/kWh; sell (feed-in) positive = revenue.

    Series values are Amber's advanced price prediction (the `forecast`
    attribute of Amber Express price sensors); the first entry is the
    current interval.
    """

    buy: Series
    sell: Series
    current_buy: float  # live price sensor states (5-min updates)
    current_sell: float
    updated_at: datetime | None = None  # oldest source last_updated, for staleness checks
    # True while the current interval's price is Amber's forecast, not the
    # AEMO-confirmed actual (the sensors' `estimate` attribute) — right after
    # an interval starts, before confirmation lands ~seconds later.
    current_estimate: bool = False


@dataclass
class BatteryState:
    soc_frac: float
    power_kw: float  # positive = charging
    capacity_kwh: float
    ts: datetime


class Action(StrEnum):
    CHARGE = "charge"  # from the grid
    DISCHARGE = "discharge"  # exporting stored energy
    IDLE = "idle"  # self-consumption territory
    # Battery not charging where self-consumption WOULD: there is PV surplus
    # and room, and the plan keeps the room (a cheaper or paid fill later).
    # The surplus is exported, or spilled under the `curtail` attribute at
    # negative feed-in. Block charging but still let the battery cover a
    # load dip. Actuate: self-consumption + battery max-charge-power 0.
    NO_CHARGE = "no_charge"
    # The mirror of NO_CHARGE: battery fully held (no charge, no discharge)
    # while the GRID serves the house — the plan prefers importing (cheap or
    # negative buy) over spending stored energy, but self-consumption would
    # discharge into the load. Only classified while the battery actually
    # has charge worth holding (above its floor).
    # Actuate: self-consumption + battery max charge AND discharge power 0.
    HOLD = "hold"
    CURTAIL = "curtail"


@dataclass
class PlanInterval:
    start: datetime
    end: datetime
    action: Action
    power_kw: float  # battery power, positive = charging
    soc_start: float
    soc_end: float
    buy: float
    sell: float
    pv_kw: float
    load_kw: float
    grid_import_kw: float
    grid_export_kw: float
    interval_cost: float
    # PV the plan actually uses this interval (the rest is curtailed);
    # 0 with pv_kw > 0 means the plan wants PV OFF (see Plan.pv_off).
    pv_used_kw: float = 0.0
    # The flow vocabulary (numbat.flow): what the energy is doing, in
    # household words, derived action-first from this interval; plus the two
    # orthogonal modifiers as they would apply to this interval (step 0
    # mirrors the plan's live-price-gated flags) and the solar thrown away.
    flow: str = ""
    export_capped: bool = False
    pv_off: bool = False
    pv_spill_kw: float = 0.0


@dataclass
class Plan:
    intervals: list[PlanInterval]
    objective_cost: float
    solver_status: str
    solve_ms: float
    computed_at: datetime
    # True when the plan was computed while the live feed-in price was above
    # spike.high_price_threshold (see Planner._live_spike); published as an
    # attribute so actuator automations can special-case spikes.
    live_spike: bool = False
    # True when the plan wants grid export WITHHELD this interval (feed-in is
    # negative and step 0 plans ~zero export) — orthogonal to the action, so
    # the actuator can cap export DURING a charge (negative buy AND feed-in;
    # the single-word action can't express both). Published as the action
    # sensor's `curtail` attribute, atomic with the action.
    curtail_export: bool = False
    # True when the plan wants PV generation STOPPED this interval: the live
    # buy price is negative (entering a cent below zero, staying while
    # negative — see planner.pv_off_wanted) and step 0 has PV available but
    # uses none of it (the house — and any charge — should draw from the
    # grid, which pays). Orthogonal to the action like `curtail`: rides
    # `hold` (paid to run the house) or `charge` (true grid charging) — or
    # idle/curtail in the edge cases of a battery at its floor or a dawn
    # trickle, equally safe since the plan uses no PV. Published as the
    # action sensor's `pv_off` attribute, atomic with the action; actuators
    # that can stop PV (Sungrow SH-T "PV power limitation") key off it.
    pv_off: bool = False
    # Plain-language explanation of step 0's action (see numbat.explain); surfaced
    # in the dashboard's "Why this action?" panel, not published as a sensor.
    explanation: dict | None = None
