"""spike.discharge_kw: raised discharge cap during confirmed spikes only."""

from datetime import UTC, datetime

import numpy as np
import pytest
from test_planner import make_settings, offline_planner, synthetic_cycle_data

from numbat.optimizer.model import (
    BatteryParams,
    GridParams,
    OptimizerConfig,
    OptimizerInputs,
    solve,
)

BATTERY = BatteryParams(
    capacity_kwh=44.8,
    max_charge_kw=9.0,
    max_discharge_kw=12.0,
    efficiency_charge=0.95,
    efficiency_discharge=0.95,
    soc_min_kwh=4.48,
    soc_max_kwh=44.8,
    wear_cost_per_kwh=0.04,
    allow_grid_charge=True,
)
GRID = GridParams(import_limit_kw=20.0, export_limit_kw=15.0)


def test_optimizer_honors_per_step_discharge_caps():
    T = 12
    buy = np.full(T, 0.30)
    sell = np.full(T, 0.10)
    buy[0], sell[0] = 8.3, 8.0  # confirmed spike right now
    buy[1], sell[1] = 8.3, 8.0  # spike forecast to continue next interval
    caps = np.full(T, 12.0)
    caps[0] = 15.0  # raised only for the confirmed current interval
    inputs = OptimizerInputs(
        dt_hours=np.full(T, 0.5),
        buy=buy,
        sell=sell,
        pv=np.zeros(T),
        load=np.full(T, 0.5),
        soc0_kwh=40.0,
        max_discharge_kw_step=caps,
    )
    config = OptimizerConfig(terminal_value=0.1, solver_timeout_s=30)
    sol = solve(inputs, BATTERY, GRID, config)
    assert sol.discharge_kw[0] == pytest.approx(15.0, abs=0.01)  # raised cap used now
    assert sol.discharge_kw[1] == pytest.approx(12.0, abs=0.01)  # everyday cap beyond


def test_planner_caps_only_on_live_spike():
    settings = make_settings(
        spike={"discharge_kw": 15.0},
        battery={"capacity_kwh": 44.8, "max_charge_kw": 9.0, "max_discharge_kw": 12.0},
    )
    planner = offline_planner(settings)
    quiet = np.full(10, 0.30)  # nothing spike-priced anywhere
    assert planner._discharge_caps(quiet, live_spike=False) is None
    caps = planner._discharge_caps(quiet, live_spike=True)
    assert caps is not None
    assert caps[0] == 15.0
    assert (caps[1:] == 12.0).all()
    # forecast steps above the release threshold anticipate the raised cap:
    # if those spikes confirm, the live cap WILL be raised when they arrive
    spiky = np.full(10, 0.30)
    spiky[4:6] = 1.50
    caps = planner._discharge_caps(spiky, live_spike=False)
    assert caps is not None
    assert caps[0] == 12.0  # no confirmed spike now
    assert (caps[4:6] == 15.0).all()
    assert (caps[1:4] == 12.0).all() and (caps[6:] == 12.0).all()


def test_planner_cap_disabled_when_not_higher():
    settings = make_settings(
        spike={"discharge_kw": 0.0},
        battery={"capacity_kwh": 44.8, "max_charge_kw": 9.0, "max_discharge_kw": 12.0},
    )
    planner = offline_planner(settings)
    assert planner._discharge_caps(np.full(10, 2.0), live_spike=True) is None


def test_plan_carries_live_spike_flag():
    settings = make_settings(optimizer={"action_switch_threshold_dollars": 0.0})
    planner = offline_planner(settings)
    data = synthetic_cycle_data(settings, live_spike=True)
    plan = planner.optimize(data, datetime(2026, 7, 15, 11, 36, 30, tzinfo=UTC))
    assert plan.live_spike is True
