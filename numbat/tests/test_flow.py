"""The flow vocabulary: household words derived action-first from the plan
(numbat.flow). Scenarios S1–S11 from the vocabulary review plus the edge
cases the power-user review found."""

from datetime import UTC, datetime, timedelta

from numbat.flow import FLOWS, annotate, details, flow_for
from numbat.models import Action, Plan, PlanInterval

START = datetime(2026, 9, 13, 3, 30, tzinfo=UTC)


def iv(action: Action, i: int = 0, **kw) -> PlanInterval:
    start = START + timedelta(minutes=30 * i)
    base = dict(
        power_kw=0.0,
        soc_start=26.0,
        soc_end=26.0,
        buy=0.30,
        sell=0.08,
        pv_kw=0.0,
        pv_used_kw=0.0,
        load_kw=1.0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        interval_cost=0.0,
    )
    base.update(kw)
    return PlanInterval(start=start, end=start + timedelta(minutes=30), action=action, **base)


def plan(*intervals, curtail=False, pv_off=False) -> Plan:
    p = Plan(list(intervals), 0.0, "optimal", 1.0, START)
    p.curtail_export = curtail
    p.pv_off = pv_off
    return p


def test_every_flow_is_reachable_and_named():
    seen = {
        flow_for(iv(Action.CHARGE, grid_import_kw=5.5, power_kw=5.0)),
        flow_for(iv(Action.DISCHARGE, grid_export_kw=12.0, power_kw=-12.0)),
        flow_for(iv(Action.HOLD, grid_import_kw=1.0)),
        flow_for(iv(Action.CURTAIL, pv_kw=8.0, pv_used_kw=1.0)),
        flow_for(iv(Action.NO_CHARGE, pv_kw=4.0, pv_used_kw=4.0, grid_export_kw=3.0)),
        flow_for(iv(Action.IDLE, pv_kw=5.0, pv_used_kw=5.0, power_kw=4.0)),
        flow_for(iv(Action.IDLE, power_kw=-2.0, load_kw=2.0)),
        flow_for(iv(Action.IDLE, pv_kw=1.0, pv_used_kw=1.0)),
        flow_for(iv(Action.IDLE, grid_import_kw=1.0)),
        flow_for(iv(Action.IDLE, load_kw=0.0)),
    }
    assert seen == set(FLOWS)


def test_scenarios_from_the_vocabulary_review():
    # S1 sunny, feed-in +8c, battery filling from the surplus
    assert flow_for(iv(Action.IDLE, pv_kw=5.0, pv_used_kw=5.0, power_kw=4.0)) == "storing_solar"
    # S3 battery full at negative feed-in: solar spilled
    assert flow_for(iv(Action.CURTAIL, pv_kw=8.0, pv_used_kw=1.0)) == "holding_back_solar"
    # S4 exporting the surplus instead of storing it
    assert (
        flow_for(iv(Action.NO_CHARGE, pv_kw=4.0, pv_used_kw=4.0, grid_export_kw=3.0))
        == "selling_solar"
    )
    # S5 evening, battery covers the house
    assert flow_for(iv(Action.IDLE, power_kw=-2.0, load_kw=2.0)) == "running_on_battery"
    # S6 2 am grid charge
    assert flow_for(iv(Action.CHARGE, grid_import_kw=5.5, power_kw=5.0)) == "charging_from_grid"
    # S7 spike export
    assert (
        flow_for(iv(Action.DISCHARGE, grid_export_kw=12.0, power_kw=-12.0))
        == "selling_stored_energy"
    )
    # S8/S10 hold: the grid serves the house, battery fenced
    assert flow_for(iv(Action.HOLD, grid_import_kw=1.0)) == "running_on_grid"
    # S11 battery at its floor at 3 am: idle, the house imports
    assert flow_for(iv(Action.IDLE, grid_import_kw=1.0)) == "battery_empty"


def test_idle_refinements_are_ordered_by_what_the_user_cares_about():
    # battery full at positive feed-in: the surplus exports under plain idle
    assert (
        flow_for(iv(Action.IDLE, pv_kw=5.0, pv_used_kw=5.0, grid_export_kw=4.0)) == "selling_solar"
    )
    # charging AND exporting (charge-rate limited): the battery filling wins
    assert (
        flow_for(iv(Action.IDLE, pv_kw=9.0, pv_used_kw=9.0, power_kw=4.0, grid_export_kw=4.0))
        == "storing_solar"
    )
    # solar exactly covering the house, battery untouched
    assert flow_for(iv(Action.IDLE, pv_kw=1.0, pv_used_kw=1.0)) == "solar_running_house"
    # solar covering only part of the house with the battery inactive at the
    # floor: the grid makes up the rest
    assert (
        flow_for(iv(Action.IDLE, pv_kw=0.4, pv_used_kw=0.4, grid_import_kw=0.6)) == "battery_empty"
    )
    # nothing at all
    assert flow_for(iv(Action.IDLE, load_kw=0.0)) == "waiting"


def test_action_wins_over_numbers():
    # a forced charge whose grid import is tiny (mostly solar) is still the
    # charge instruction — the label must not say "storing solar"
    assert (
        flow_for(iv(Action.CHARGE, pv_kw=4.0, pv_used_kw=4.0, power_kw=5.0, grid_import_kw=2.0))
        == "charging_from_grid"
    )
    # hold at night with no solar is still running on the grid, not "waiting"
    assert flow_for(iv(Action.HOLD, grid_import_kw=0.8, load_kw=0.8)) == "running_on_grid"
    # a discharge mostly eaten by the house is still the discharge instruction
    assert (
        flow_for(iv(Action.DISCHARGE, power_kw=-5.0, load_kw=4.9, grid_export_kw=0.1))
        == "selling_stored_energy"
    )


def test_annotate_fills_modifiers_and_spill_per_interval():
    p = plan(
        # S2: storing the surplus at negative feed-in — capped, nothing spilled
        iv(Action.IDLE, 0, pv_kw=5.0, pv_used_kw=5.0, power_kw=4.0, sell=-0.03),
        # S3: full battery, solar spilled
        iv(Action.CURTAIL, 1, pv_kw=8.0, pv_used_kw=1.0, sell=-0.03),
        # S8: paid to import, PV off, everything spilled
        iv(Action.HOLD, 2, pv_kw=8.0, pv_used_kw=0.0, buy=-0.13, sell=-0.05, grid_import_kw=1.0),
        # shallow negative buy right after a deep one: the forward pass keeps
        # PV off (the actuator would), as pv_off_wanted's asymmetry does live
        iv(Action.HOLD, 3, pv_kw=8.0, pv_used_kw=0.0, buy=-0.005, sell=-0.05, grid_import_kw=1.0),
        # night hold: no solar, nothing to cap or switch off
        iv(Action.HOLD, 4, buy=0.12, sell=0.05, grid_import_kw=1.0),
        curtail=True,
    )
    annotate(p)
    s2, s3, s8, shallow, night = p.intervals
    assert (s2.flow, s2.export_capped, s2.pv_off, s2.pv_spill_kw) == (
        "storing_solar",
        True,
        False,
        0.0,
    )
    assert (s3.flow, s3.export_capped, s3.pv_spill_kw) == ("holding_back_solar", True, 7.0)
    assert (s8.flow, s8.export_capped, s8.pv_off, s8.pv_spill_kw) == (
        "running_on_grid",
        True,
        True,
        8.0,
    )
    assert shallow.pv_off is True
    assert (night.export_capped, night.pv_off, night.pv_spill_kw) == (False, False, 0.0)


def test_no_charge_reads_as_what_the_surplus_does():
    # exported surplus: selling solar; spilled surplus (cap on): holding back
    exported = iv(Action.NO_CHARGE, pv_kw=4.0, pv_used_kw=4.0, grid_export_kw=3.0)
    assert flow_for(exported) == "selling_solar"
    assert flow_for(iv(Action.NO_CHARGE, pv_kw=4.0, pv_used_kw=1.0)) == "holding_back_solar"


def test_curtail_at_positive_feed_in_is_an_export_limit_cut():
    # the export limit, not a negative price, spills the solar: the flow is
    # still holding_back_solar but nothing is "capped" by price
    p = plan(iv(Action.CURTAIL, 0, pv_kw=9.0, pv_used_kw=6.0, grid_export_kw=5.0, sell=0.08))
    annotate(p)
    s0 = p.intervals[0]
    assert (s0.flow, s0.export_capped, s0.pv_spill_kw) == ("holding_back_solar", False, 3.0)


def test_pv_off_per_interval_is_a_forward_pass_with_the_planners_asymmetry():
    # −3c enters, −0.5c stays on (the actuator would hold), +1c leaves, and
    # −0.5c alone does not re-enter
    prices = [-0.03, -0.005, 0.01, -0.005, -0.03]
    p = plan(
        *[iv(Action.HOLD, i, pv_kw=6.0, pv_used_kw=0.0, buy=b, grid_import_kw=1.0)
          for i, b in enumerate(prices)],
        pv_off=True,
    )
    annotate(p)
    assert [x.pv_off for x in p.intervals] == [True, True, False, False, True]


def test_annotate_step0_mirrors_the_plans_live_gated_flags():
    # forecast prices say "cap" but the live-gated plan flag says no (e.g. the
    # live feed-in came in positive): step 0 shows the published truth
    p = plan(
        iv(Action.IDLE, 0, pv_kw=5.0, pv_used_kw=5.0, power_kw=4.0, sell=-0.03),
        iv(Action.IDLE, 1, pv_kw=5.0, pv_used_kw=5.0, power_kw=4.0, sell=-0.03),
        curtail=False,
        pv_off=False,
    )
    annotate(p)
    assert p.intervals[0].export_capped is False
    assert p.intervals[1].export_capped is True
    # tiny spill (sensor noise) is not "solar thrown away"
    q = plan(iv(Action.IDLE, 0, pv_kw=5.0, pv_used_kw=4.98, power_kw=4.0))
    annotate(q)
    assert q.intervals[0].pv_spill_kw == 0.0


def test_details_quote_the_look_ahead_facts():
    p = plan(
        iv(Action.HOLD, 0, buy=0.12, grid_import_kw=1.0, soc_end=30.0),
        iv(Action.HOLD, 1, buy=0.12, grid_import_kw=1.0, soc_end=30.0),
        iv(Action.IDLE, 2, buy=0.38, power_kw=-2.0, soc_end=29.0),
        iv(Action.IDLE, 3, buy=0.38, power_kw=-2.0, soc_end=28.0),
        iv(Action.IDLE, 4, buy=0.05, pv_kw=6.0, pv_used_kw=6.0, power_kw=5.0, soc_end=30.5),
    )
    annotate(p)
    d = details(p, capacity_kwh=44.8)
    assert d["key"] == "running_on_grid"
    assert d["next_use_time"] == p.intervals[2].start.isoformat()
    assert d["next_use_kind"] == "house"
    assert d["next_use_price"] == 0.38
    assert d["next_use_buy"] == 0.38
    assert d["next_fill_time"] == p.intervals[4].start.isoformat()
    assert d["next_fill_source"] == "solar"
    # five half-hour steps don't cover 12 h: no floor claim
    assert "soc_min_ahead_pct" not in d
    long = plan(*[iv(Action.IDLE, i, power_kw=-1.0, soc_end=30.0 - i * 0.5) for i in range(26)])
    annotate(long)
    assert details(long, 44.8)["soc_min_ahead_pct"] == round(100 * (30.0 - 23 * 0.5) / 44.8, 1)
    # a forced export ahead quotes the feed-in price it earns
    sale = plan(
        iv(Action.HOLD, 0, buy=0.12, grid_import_kw=1.0),
        iv(Action.DISCHARGE, 1, buy=0.40, sell=0.60, power_kw=-8.0, grid_export_kw=7.0),
    )
    annotate(sale)
    ds = details(sale, 44.8)
    assert (ds["next_use_kind"], ds["next_use_price"]) == ("export", 0.60)
    # a grid fill is named as such; nothing ahead means no keys
    g = plan(
        iv(Action.IDLE, 0, grid_import_kw=1.0),
        iv(Action.CHARGE, 1, power_kw=5.0, grid_import_kw=6.0),
    )
    annotate(g)
    assert details(g, 44.8)["next_fill_source"] == "grid"
    assert details(g, 44.8)["next_fill_price"] == 0.30
    assert "next_fill_price" not in d  # a solar fill has no price
    lone = plan(iv(Action.IDLE, 0, load_kw=0.0))
    annotate(lone)
    assert "next_fill_time" not in details(lone, 44.8)
    assert details(plan(), 44.8) is None
