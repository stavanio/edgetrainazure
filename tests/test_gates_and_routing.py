"""Tests for promotion gates, labeling triage, and on-robot scoring.

The gate tests encode the promises the safety case makes. If one of these starts
failing, the fix is almost never to relax the test.
"""

from __future__ import annotations

import numpy as np
import pytest

from edgeforge.evaluation.gates import (
    AggregateMetrics,
    ClosedLoopMetrics,
    EvaluationResult,
    Tolerances,
    Verdict,
    evaluate_all,
    tier2_slices,
    tier3_closed_loop,
)
from edgeforge.labeling.autolabel_teacher import (
    Detection,
    Prediction,
    Route,
    RoutingPolicy,
    agree,
    iou,
    label_confidence_weights,
    route,
    route_batch,
)
from edgeforge.taxonomy import Actors, Cell, Geometry, Illumination, Particulate, Surface

SAFETY_CELL = Cell(
    Illumination.DARK, Particulate.HEAVY, Surface.DRY_COMPACT, Geometry.DRIFT, Actors.PERSONNEL
)
COMMON_CELL = Cell(
    Illumination.BRIGHT, Particulate.CLEAR, Surface.DRY_COMPACT, Geometry.DRIFT, Actors.NONE
)


def aggregate(map_=42.0, miou=71.0, ece=0.03) -> AggregateMetrics:
    return AggregateMetrics(map_50_95=map_, miou=miou, expected_calibration_error=ece)


def closed_loop(**kw) -> ClosedLoopMetrics:
    base = dict(
        time_to_detect_s=0.42,
        time_to_brake_margin_s=1.8,
        false_stops_per_hour=0.30,
        missed_personnel_events=0,
        id_switches_per_1k=4.2,
        scenarios_run=2_000,
    )
    base.update(kw)
    return ClosedLoopMetrics(**base)


def result(**kw) -> EvaluationResult:
    base = dict(
        model="hazard-seg",
        version=41,
        aggregate=aggregate(),
        per_slice={COMMON_CELL.key: 55.0, SAFETY_CELL.key: 38.0},
        closed_loop=closed_loop(),
        snapshot_merkle_root="abc123",
        golden_set_version="v7",
    )
    base.update(kw)
    return EvaluationResult(**base)


# --- tier 3: the absolutes ---------------------------------------------------


def test_one_missed_personnel_event_blocks_promotion():
    outcomes = tier3_closed_loop(
        closed_loop(missed_personnel_events=1), closed_loop(), Tolerances()
    )
    missed = next(o for o in outcomes if o.gate == "missed_personnel")
    assert missed.verdict is Verdict.FAIL
    assert missed.blocking


def test_missed_personnel_is_absolute_not_comparative():
    # Even if the incumbent was worse, one miss still blocks.
    outcomes = tier3_closed_loop(
        closed_loop(missed_personnel_events=1),
        closed_loop(missed_personnel_events=5),
        Tolerances(),
    )
    assert next(o for o in outcomes if o.gate == "missed_personnel").blocking


def test_a_shrunken_replay_suite_is_not_evidence():
    outcomes = tier3_closed_loop(closed_loop(scenarios_run=10), None, Tolerances())
    assert next(o for o in outcomes if o.gate == "scenario_coverage").blocking


def test_nuisance_stops_are_bounded():
    # Operators learn to override a model that cries wolf, and an overridden
    # safety model protects nobody.
    outcomes = tier3_closed_loop(
        closed_loop(false_stops_per_hour=0.40), closed_loop(false_stops_per_hour=0.30), Tolerances()
    )
    assert next(o for o in outcomes if o.gate == "false_stops").blocking


def test_slower_detection_blocks_even_with_better_pixels():
    outcomes = tier3_closed_loop(
        closed_loop(time_to_detect_s=0.90), closed_loop(time_to_detect_s=0.42), Tolerances()
    )
    assert next(o for o in outcomes if o.gate == "time_to_detect").blocking


# --- tier 2: the slice gate that earns its keep ------------------------------


def test_aggregate_gain_cannot_mask_a_safety_slice_regression():
    """The central promise of the whole evaluation design.

    Candidate is dramatically better overall -- and worse on the one cell where
    a person is in front of the machine in poor visibility. It must not promote.
    """
    incumbent_slices = {COMMON_CELL.key: 55.0, SAFETY_CELL.key: 38.0}
    candidate_slices = {COMMON_CELL.key: 72.0, SAFETY_CELL.key: 37.4}

    res = evaluate_all(
        result(aggregate=aggregate(map_=58.0), per_slice=candidate_slices),
        incumbent_aggregate=aggregate(map_=42.0),
        incumbent_slices=incumbent_slices,
        incumbent_closed_loop=closed_loop(),
    )

    assert not res.passed
    failed = [o.gate for o in res.outcomes if o.blocking]
    assert f"slice::{SAFETY_CELL.key}" in failed


def test_ordinary_slice_has_headroom():
    outcomes = tier2_slices({COMMON_CELL.key: 54.0}, {COMMON_CELL.key: 55.0})
    assert all(o.verdict is Verdict.PASS for o in outcomes)


def test_ordinary_slice_beyond_tolerance_still_blocks():
    outcomes = tier2_slices({COMMON_CELL.key: 51.0}, {COMMON_CELL.key: 55.0})
    assert any(o.blocking for o in outcomes)


def test_a_vanished_slice_blocks():
    # Usually means the golden set lost coverage, which is its own problem.
    outcomes = tier2_slices({COMMON_CELL.key: 55.0}, {COMMON_CELL.key: 55.0, SAFETY_CELL.key: 38.0})
    assert any(o.gate.startswith("slice_missing::") and o.blocking for o in outcomes)


def test_first_release_has_no_incumbent_to_compare_against():
    res = evaluate_all(result())
    assert res.passed


# --- tier 1 ------------------------------------------------------------------


def test_calibration_is_an_absolute_bar():
    # An overconfident model in a whiteout is dangerous regardless of the
    # incumbent's behaviour.
    res = evaluate_all(
        result(aggregate=aggregate(ece=0.20)),
        incumbent_aggregate=aggregate(ece=0.30),
        incumbent_slices={COMMON_CELL.key: 55.0, SAFETY_CELL.key: 38.0},
        incumbent_closed_loop=closed_loop(),
    )
    assert not res.passed
    assert any(o.gate == "calibration_ece" and o.blocking for o in res.outcomes)


def test_dev_waives_failures_but_still_records_them():
    res = evaluate_all(
        result(closed_loop=closed_loop(missed_personnel_events=3)),
        incumbent_closed_loop=closed_loop(),
        blocking=False,
    )
    assert res.passed
    waived = [o for o in res.outcomes if o.verdict is Verdict.WAIVED]
    assert waived and "advisory in dev" in waived[0].detail


def test_result_serialises_with_verdicts():
    import json

    res = evaluate_all(result())
    doc = json.loads(res.to_json())
    assert doc["passed"] is True
    assert doc["snapshot_merkle_root"] == "abc123"


# --- labeling triage ---------------------------------------------------------


def det(cls="hazard", score=0.9, box=(0.1, 0.1, 0.3, 0.3)) -> Detection:
    return Detection(cls=cls, score=score, box=box)


def test_iou_basics():
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == pytest.approx(1.0)
    assert iou((0, 0, 1, 1), (2, 2, 3, 3)) == 0.0


def test_agreement_requires_matching_counts():
    policy = RoutingPolicy()
    t = Prediction("f", [det(), det(box=(0.5, 0.5, 0.7, 0.7))])
    s = Prediction("f", [det()])
    assert not agree(t, s, policy)


def test_confident_agreement_auto_accepts():
    t = Prediction("f", [det(score=0.95)])
    s = Prediction("f", [det(score=0.88)])
    assert route(t, s) is Route.AUTO_ACCEPT


def test_disagreement_routes_to_human_review():
    # The highest-value labeling queue in the system: frames on the decision
    # boundary between the teacher and what the fleet currently believes.
    t = Prediction("f", [det(score=0.95)])
    s = Prediction("f", [det(score=0.9, box=(0.8, 0.8, 0.95, 0.95))])
    assert route(t, s) is Route.HUMAN_REVIEW


def test_unconfident_teacher_routes_to_a_human_drawing_from_scratch():
    t = Prediction("f", [det(score=0.31)])
    assert route(t, None) is Route.HUMAN_LABEL


def test_personnel_always_gets_two_humans_even_on_perfect_agreement():
    """Agreement between two models trained on the same data is not independent
    evidence, so it does not earn a discount on the case that can injure someone."""
    t = Prediction("f", [det(cls="personnel", score=0.99)])
    s = Prediction("f", [det(cls="personnel", score=0.99)])
    assert route(t, s) is Route.HUMAN_REDUNDANT


def test_personnel_seen_only_by_the_student_still_escalates():
    t = Prediction("f", [det(cls="hazard", score=0.95)])
    s = Prediction("f", [det(cls="personnel", score=0.6)])
    assert route(t, s) is Route.HUMAN_REDUNDANT


def test_auto_accepted_labels_are_down_weighted():
    # Otherwise teacher errors become permanent student errors.
    routes = {"a": Route.AUTO_ACCEPT, "b": Route.HUMAN_REDUNDANT}
    weights = label_confidence_weights(routes)
    assert weights["a"] < weights["b"] == 1.0


def test_route_batch_reports_the_auto_accept_rate():
    preds = [Prediction(f"f{i}", [det(score=0.95)]) for i in range(4)]
    students = {f"f{i}": Prediction(f"f{i}", [det(score=0.9)]) for i in range(3)}
    routes, stats = route_batch(preds, students)
    assert stats.total == 4
    assert stats.auto_accept_rate == pytest.approx(0.75)
    assert routes["f3"] is Route.HUMAN_REVIEW


# --- on-robot scoring --------------------------------------------------------


def test_curator_prioritises_and_sheds():
    from edge_modules.curator.scoring import (
        Curator,
        DeployedContext,
        FrameSignals,
        ScoringWeights,
        Tier,
        upload_order,
    )

    ctx = DeployedContext(
        dataset_centroid=np.array([1.0, 0.0, 0.0]),
        feature_mean=np.zeros(3),
        feature_precision=np.eye(3),
    )
    curator = Curator(ScoringWeights(), ctx)

    familiar = FrameSignals(
        "familiar",
        0,
        logits=np.array([9.0, 0.1, 0.1]),
        penultimate=np.array([1.0, 0.0, 0.0]),
        haze=0.1,
        geometric_prior_disagrees=False,
    )
    novel = FrameSignals(
        "novel",
        10**11,
        logits=np.array([1.0, 1.0, 1.0]),
        penultimate=np.array([0.0, 6.0, 6.0]),
        haze=0.1,
        geometric_prior_disagrees=True,
    )
    event = FrameSignals(
        "event",
        2 * 10**11,
        logits=np.array([9.0, 0.1, 0.1]),
        penultimate=np.array([1.0, 0.0, 0.0]),
        haze=0.1,
        geometric_prior_disagrees=False,
        safety_event=True,
    )
    whiteout = FrameSignals(
        "whiteout",
        3 * 10**11,
        logits=np.array([1.0, 1.0, 1.0]),
        penultimate=np.array([0.0, 6.0, 6.0]),
        haze=0.95,
        geometric_prior_disagrees=True,
    )

    decisions = [curator.score(s) for s in (familiar, novel, event, whiteout)]
    by_id = {d.frame_id: d for d in decisions}

    assert by_id["event"].tier is Tier.EVENT
    assert by_id["novel"].tier is Tier.INTERESTING
    # Uninterpretable frames are dropped on the robot: the cloud gate would
    # reject them anyway, so uploading wastes the uplink twice.
    assert by_id["whiteout"].tier is Tier.DROP

    order = upload_order(decisions)
    assert order[0].frame_id == "event"
    assert "whiteout" not in {d.frame_id for d in order}


def test_safety_event_survives_even_a_dust_cloud():
    from edge_modules.curator.scoring import (
        Curator,
        DeployedContext,
        FrameSignals,
        ScoringWeights,
        Tier,
    )

    ctx = DeployedContext(np.array([1.0, 0.0]), np.zeros(2), np.eye(2))
    curator = Curator(ScoringWeights(), ctx)
    s = FrameSignals(
        "e",
        0,
        logits=np.array([5.0, 5.0]),
        penultimate=np.array([1.0, 0.0]),
        haze=0.99,
        geometric_prior_disagrees=False,
        safety_event=True,
    )
    assert curator.score(s).tier is Tier.EVENT


def test_twin_patch_retunes_without_a_deployment():
    from edge_modules.curator.scoring import Curator, DeployedContext, ScoringWeights

    ctx = DeployedContext(np.array([1.0, 0.0]), np.zeros(2), np.eye(2))
    curator = Curator(ScoringWeights(), ctx)
    curator.apply_twin_patch({"interesting_threshold": 0.2, "not_a_real_property": 1})
    assert curator.weights.interesting_threshold == pytest.approx(0.2)
    # An unknown property must not cause the whole patch to be rejected, or a
    # robot ends up stuck on stale thresholds.
    assert curator.weights.entropy == ScoringWeights().entropy
