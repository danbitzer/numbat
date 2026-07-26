from datetime import UTC, datetime

import pytest

from numbat.adapters.sungrow import BatteryParseError, parse_power_kw, parse_soc_fraction
from numbat.ha.client import State

TS = datetime(2026, 7, 15, 11, 20, tzinfo=UTC)


def state(entity_id: str, value: str, unit: str = "") -> State:
    attrs = {"unit_of_measurement": unit} if unit else {}
    return State(entity_id=entity_id, state=value, attributes=attrs, last_updated=TS)


def test_soc_percent_unit():
    assert parse_soc_fraction(state("sensor.battery_level", "72.5", "%")) == pytest.approx(0.725)


def test_soc_percent_inferred_without_unit():
    assert parse_soc_fraction(state("sensor.battery_level", "72.5")) == pytest.approx(0.725)


def test_soc_ambiguous_low_value_without_unit_rejected():
    """0.72 without a unit could be 72% (fraction) or a nearly-flat battery at
    0.72% — guessing wrong discharges an empty battery, so refuse."""
    with pytest.raises(BatteryParseError, match="ambiguous"):
        parse_soc_fraction(state("sensor.battery_level", "0.72"))


def test_soc_out_of_range_rejected():
    with pytest.raises(BatteryParseError, match="outside"):
        parse_soc_fraction(state("sensor.battery_level", "142", "%"))


def test_power_watts_converted():
    assert parse_power_kw(state("sensor.battery_power", "2500", "W"), True) == pytest.approx(2.5)


def test_power_kw_passthrough():
    assert parse_power_kw(state("sensor.battery_power", "-3.2", "kW"), True) == pytest.approx(-3.2)


def test_power_missing_unit_rejected():
    with pytest.raises(BatteryParseError, match="not recognised"):
        parse_power_kw(state("sensor.battery_power", "2500"), True)


def test_power_null_unit_attribute_rejected_cleanly():
    s = State(
        entity_id="sensor.battery_power",
        state="2500",
        attributes={"unit_of_measurement": None},
        last_updated=TS,
    )
    with pytest.raises(BatteryParseError, match="not recognised"):
        parse_power_kw(s, True)


def test_power_sign_convention_flip():
    # Sensor reads negative while charging on charge_negative installs
    assert parse_power_kw(state("sensor.battery_power", "-2500", "W"), False) == pytest.approx(2.5)


def test_power_weird_unit_rejected():
    with pytest.raises(BatteryParseError, match="unit"):
        parse_power_kw(state("sensor.battery_power", "5", "A"), True)
