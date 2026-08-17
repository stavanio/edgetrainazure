"""Tests for SLI evaluation, error budgets, burn rate, and release posture.

The posture tests are the important ones. An error budget nobody enforces is a
number on a dashboard; these encode the rule that makes it a decision.
"""

from __future__ import annotations

import pytest

from edgeforge.fleet.slo import (
    Kind,
    Observation,
    Posture,
    Severity,
    Status,
    check_invariants,
    evaluate,
    evaluate_burn_rate,
    load,
    release_posture,
    render_report,
)


@pytest.fixture(scope="module")
def catalogue():
    return load()


# --- catalogue integrity -----------------------------------------------------


def test_catalogue_loads_and_is_complete(catalogue):
    assert len(catalogue.invariants) == 4
    assert len(catalogue.slos) == 21  # B1-B7, C1-C7, D1-D7
    assert {s.group for s in catalogue.slos} == {"fleet", "pipeline", "efficiency"}


def test_slo_ids_are_unique(catalogue):
    ids = [s.id for s in catalogue.slos] + [i.id for i in catalogue.invariants]
    assert len(ids) == len(set(ids))


def test_every_slo_names_its_numerator_and_denominator(catalogue):
    """An 'availability' SLI with an unstated denominator is not an SLI."""
    for slo in catalogue.slos:
        if slo.kind is Kind.FRACTION:
            assert slo.target is not None, slo.id
            assert 0 < slo.target <= 1.0, slo.id
        elif slo.kind is Kind.RATIO_THRESHOLD:
            assert slo.target_max is not None and slo.target_max > 0, slo.id
        else:
            assert slo.target_slope_max is not None, slo.id


def test_every_slo_has_a_query(catalogue):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "observability"
    for slo in catalogue.slos:
        assert slo.query, slo.id
        assert (root / slo.query).exists(), f"{slo.id} references missing {slo.query}"


def test_safety_indicators_are_invariants_not_slos(catalogue):
    """Expressing 'no missed personnel' as a percentage would make it negotiable."""
    names = {i.name for i in catalogue.invariants}
    assert "missed_personnel" in names
    assert not any(s.group == "safety" for s in catalogue.slos)


def test_windows_parse(catalogue):
    assert catalogue.slo("B1").window_days == 28
    assert catalogue.slo("B6").window_days == 90


# --- fraction SLIs -----------------------------------------------------------


def test_meeting_objective_leaves_budget(catalogue):
    b1 = catalogue.slo("B1")  # target 0.995
    ev = evaluate(b1, Observation("B1", good=99_900, valid=100_000))
    assert ev.status is Status.MEETING
    assert ev.observed == pytest.approx(0.999)
    # 0.1% error against a 0.5% budget = 20% consumed, 80% remaining
    assert ev.budget_remaining_pct == pytest.approx(80.0)
    assert ev.burn_rate == pytest.approx(0.2)


def test_breaching_objective_exhausts_budget(catalogue):
    ev = evaluate(catalogue.slo("B1"), Observation("B1", good=98_000, valid=100_000))
    assert ev.status is Status.BREACHING
    assert ev.budget_remaining_pct == 0.0
    assert ev.burn_rate == pytest.approx(4.0)


def test_meeting_but_nearly_out_of_budget_is_at_risk(catalogue):
    # 0.4% error against a 0.5% budget: still meeting, 20% budget left.
    ev = evaluate(catalogue.slo("B1"), Observation("B1", good=99_600, valid=100_000))
    assert ev.status is Status.AT_RISK
    assert ev.meeting


def test_target_of_one_has_no_budget_by_construction(catalogue):
    """C5/C6 target 1.0. Any failure is 0% remaining -- intended, not a bug."""
    c6 = catalogue.slo("C6")
    assert c6.target == 1.0
    clean = evaluate(c6, Observation("C6", good=40, valid=40))
    assert clean.status is Status.MEETING
    assert clean.budget_remaining_pct == 100.0

    dirty = evaluate(c6, Observation("C6", good=39, valid=40))
    assert dirty.status is Status.BREACHING
    assert dirty.budget_remaining_pct == 0.0


def test_empty_window_is_no_data_not_zero_percent(catalogue):
    ev = evaluate(catalogue.slo("B1"), Observation("B1", good=0, valid=0))
    assert ev.status is Status.NO_DATA
    assert ev.observed is None
    # No data must not read as a breach -- it would freeze releases on a
    # telemetry outage, which is exactly the wrong response.
    assert ev.meeting


# --- ratio-threshold SLIs ----------------------------------------------------


def test_cost_under_ceiling_passes(catalogue):
    d2 = catalogue.slo("D2")  # ceiling $0.32
    ev = evaluate(d2, Observation("D2", numerator=25_000, denominator=100_000))
    assert ev.status is Status.MEETING
    assert ev.observed == pytest.approx(0.25)


def test_cost_over_ceiling_breaches(catalogue):
    ev = evaluate(catalogue.slo("D2"), Observation("D2", numerator=40_000, denominator=100_000))
    assert ev.status is Status.BREACHING
    assert ev.budget_remaining_pct == 0.0


def test_cost_close_to_ceiling_is_at_risk(catalogue):
    ev = evaluate(catalogue.slo("D2"), Observation("D2", numerator=31_000, denominator=100_000))
    assert ev.status is Status.AT_RISK


# --- trend SLIs --------------------------------------------------------------


def test_falling_unit_cost_meets_the_objective(catalogue):
    """D7 is the board number: the claim is that unit economics improve with scale."""
    series = tuple((float(d), 1650.0 - 4.0 * d) for d in range(0, 90, 10))
    ev = evaluate(catalogue.slo("D7"), Observation("D7", series=series))
    assert ev.status is Status.MEETING
    assert ev.observed == pytest.approx(-4.0)
    assert "falling" in ev.detail


def test_rising_unit_cost_breaches(catalogue):
    series = tuple((float(d), 1650.0 + 6.0 * d) for d in range(0, 90, 10))
    ev = evaluate(catalogue.slo("D7"), Observation("D7", series=series))
    assert ev.status is Status.BREACHING
    assert "rising" in ev.detail


def test_trend_needs_enough_points(catalogue):
    ev = evaluate(catalogue.slo("D7"), Observation("D7", series=((0.0, 1.0), (1.0, 2.0))))
    assert ev.status is Status.NO_DATA


def test_trend_with_no_time_spread_is_no_data(catalogue):
    flat = ((5.0, 1.0), (5.0, 2.0), (5.0, 3.0))
    ev = evaluate(catalogue.slo("D7"), Observation("D7", series=flat))
    assert ev.status is Status.NO_DATA


# --- burn rate ---------------------------------------------------------------


def test_fast_burn_pages(catalogue):
    b1 = catalogue.slo("B1")  # budget 0.5%
    # 10% error rate = 20x burn: over the 14.4x page threshold in both windows.
    alerts = evaluate_burn_rate(
        b1,
        catalogue.burn_rate_policy,
        long_window_sli={"PT1H": 0.90, "PT6H": 0.90, "P1D": 0.90, "P3D": 0.90},
        short_window_sli={"PT5M": 0.90, "PT30M": 0.90, "PT2H": 0.90, "PT6H": 0.90},
    )
    assert alerts
    assert alerts[0].severity is Severity.PAGE
    assert alerts[0].rule_burn_rate == 14.4


def test_short_window_recovery_stops_the_page(catalogue):
    """The whole point of the short window: an alert must not persist for hours
    after the problem resolved, and the long window stays hot long after."""
    alerts = evaluate_burn_rate(
        catalogue.slo("B1"),
        catalogue.burn_rate_policy,
        long_window_sli={"PT1H": 0.90, "PT6H": 0.90, "P1D": 0.90, "P3D": 0.90},
        short_window_sli={"PT5M": 1.0, "PT30M": 1.0, "PT2H": 1.0, "PT6H": 1.0},
    )
    assert alerts == []


def test_slow_burn_tickets_rather_than_pages(catalogue):
    # 1.5% error against a 0.5% budget = 3x: ticket tier, not page tier.
    sli = 0.985
    alerts = evaluate_burn_rate(
        catalogue.slo("B1"),
        catalogue.burn_rate_policy,
        long_window_sli=dict.fromkeys(["PT1H", "PT6H", "P1D", "P3D"], sli),
        short_window_sli=dict.fromkeys(["PT5M", "PT30M", "PT2H", "PT6H"], sli),
    )
    assert alerts
    assert all(a.severity is Severity.TICKET for a in alerts)


def test_healthy_sli_fires_nothing(catalogue):
    alerts = evaluate_burn_rate(
        catalogue.slo("B1"),
        catalogue.burn_rate_policy,
        long_window_sli=dict.fromkeys(["PT1H", "PT6H", "P1D", "P3D"], 0.9999),
        short_window_sli=dict.fromkeys(["PT5M", "PT30M", "PT2H", "PT6H"], 0.9999),
    )
    assert alerts == []


def test_burn_rate_does_not_apply_to_cost_or_trend_slis(catalogue):
    for slo_id in ("D2", "D7"):
        assert (
            evaluate_burn_rate(
                catalogue.slo(slo_id),
                catalogue.burn_rate_policy,
                long_window_sli={"PT1H": 0.5},
                short_window_sli={"PT5M": 0.5},
            )
            == []
        )


# --- invariants --------------------------------------------------------------


def test_any_nonzero_invariant_count_is_a_breach(catalogue):
    breaches = check_invariants(catalogue, {"missed_personnel": 1})
    assert len(breaches) == 1
    assert breaches[0].invariant.id == "A1"
    assert breaches[0].invariant.blocks_releases


def test_clean_fleet_has_no_breaches(catalogue):
    assert check_invariants(catalogue, {"missed_personnel": 0, "safety_envelope": 0}) == []


# --- release posture: the rule that makes budgets mean something -------------


def test_all_budgets_healthy_ships(catalogue):
    evs = [
        evaluate(catalogue.slo(i), Observation(i, good=99_990, valid=100_000))
        for i in ("B1", "B2", "B3")
    ]
    decision = release_posture(catalogue, evs)
    assert decision.posture is Posture.SHIP


def test_low_budget_constrains(catalogue):
    # 0.3% error against a 0.5% budget = 40% remaining: below healthy(50), above
    # constrained(10).
    evs = [evaluate(catalogue.slo("B1"), Observation("B1", good=99_700, valid=100_000))]
    decision = release_posture(catalogue, evs)
    assert decision.posture is Posture.CONSTRAINED
    assert "B1" in decision.constrained_slos


def test_exhausted_budget_freezes(catalogue):
    evs = [evaluate(catalogue.slo("B1"), Observation("B1", good=99_000, valid=100_000))]
    decision = release_posture(catalogue, evs)
    assert decision.posture is Posture.FREEZE
    assert "B1" in decision.exhausted_slos
    assert "reliability work takes priority" in decision.reason


def test_invariant_breach_overrides_perfectly_healthy_budgets(catalogue):
    """Ordering matters: a missed person freezes releases no matter how good
    every reliability number looks."""
    evs = [
        evaluate(catalogue.slo(i), Observation(i, good=100_000, valid=100_000))
        for i in ("B1", "B2", "B3")
    ]
    breaches = check_invariants(catalogue, {"missed_personnel": 1})
    decision = release_posture(catalogue, evs, breaches)
    assert decision.posture is Posture.FREEZE
    assert decision.blocking_invariants == ["A1"]


def test_non_blocking_invariant_constrains_rather_than_freezing(catalogue):
    # A3 rolls back a ring; it does not freeze the whole fleet's pipeline.
    breaches = check_invariants(catalogue, {"safety_envelope": 2})
    decision = release_posture(catalogue, [], breaches)
    assert decision.posture is Posture.CONSTRAINED
    assert "A3" in decision.blocking_invariants


def test_telemetry_outage_does_not_freeze_releases(catalogue):
    """NO_DATA must not read as a breach -- freezing on a telemetry outage is
    exactly the wrong response, and it is a failure mode teams hit in practice."""
    evs = [evaluate(catalogue.slo("B1"), Observation("B1", good=0, valid=0))]
    assert release_posture(catalogue, evs).posture is Posture.SHIP


# --- reporting ---------------------------------------------------------------


def test_report_renders_posture_groups_and_breaches(catalogue):
    evs = [
        evaluate(catalogue.slo("B1"), Observation("B1", good=99_900, valid=100_000)),
        evaluate(catalogue.slo("D2"), Observation("D2", numerator=25_000, denominator=100_000)),
    ]
    breaches = check_invariants(catalogue, {"missed_personnel": 1})
    text = render_report(evs, release_posture(catalogue, evs, breaches), breaches)

    assert "RELEASE POSTURE: FREEZE" in text
    assert "INVARIANT BREACHES" in text
    assert "perception_availability" in text
    assert "$0.25" in text  # cost formatted as currency, not a percentage
    assert "99.900%" in text  # fraction formatted as a percentage
