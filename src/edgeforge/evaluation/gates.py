"""Promotion gates.

Three tiers, all of which must pass. Passing does not deploy anything -- it
registers the model as `Evaluated` and lets a human decide.

  Tier 1  aggregate metrics on the human-only golden set
  Tier 2  per-slice metrics; no slice may regress beyond its tolerance
  Tier 3  closed-loop decision outcomes on the replay suite

Tier 2 is the one that earns its keep. A model can gain aggregate mAP while
getting worse at the only condition that can hurt someone, because the fleet's
distribution is dominated by empty, well-lit main drifts and the aggregate
metric follows that distribution. Tier 2 is what stops it.

Missed-personnel is not a threshold; it is an absolute. One instance blocks
promotion and the frame joins the golden set permanently.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from edgeforge.taxonomy import Cell, slice_tolerance

log = logging.getLogger(__name__)


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WAIVED = "waived"  # dev only; gates are advisory there


@dataclass(slots=True)
class GateOutcome:
    gate: str
    tier: int
    verdict: Verdict
    detail: str
    observed: float | None = None
    threshold: float | None = None

    @property
    def blocking(self) -> bool:
        return self.verdict is Verdict.FAIL


@dataclass(slots=True)
class AggregateMetrics:
    map_50_95: float
    miou: float
    expected_calibration_error: float


@dataclass(slots=True)
class ClosedLoopMetrics:
    """Decision-level outcomes from the replay suite. See evaluation/closed_loop.py."""

    time_to_detect_s: float  # lower is better
    time_to_brake_margin_s: float  # higher is better
    false_stops_per_hour: float  # lower is better
    missed_personnel_events: int  # must be zero
    id_switches_per_1k: float  # lower is better
    scenarios_run: int


@dataclass(slots=True)
class EvaluationResult:
    model: str
    version: int
    aggregate: AggregateMetrics
    per_slice: dict[str, float]  # cell.key -> mAP
    closed_loop: ClosedLoopMetrics
    snapshot_merkle_root: str
    golden_set_version: str
    outcomes: list[GateOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(o.blocking for o in self.outcomes)

    def to_json(self) -> str:
        return json.dumps(
            {
                "model": self.model,
                "version": self.version,
                "passed": self.passed,
                "aggregate": asdict(self.aggregate),
                "closed_loop": asdict(self.closed_loop),
                "per_slice": self.per_slice,
                "snapshot_merkle_root": self.snapshot_merkle_root,
                "golden_set_version": self.golden_set_version,
                "outcomes": [asdict(o) for o in self.outcomes],
            },
            indent=2,
            sort_keys=True,
            default=str,
        )


@dataclass(frozen=True, slots=True)
class Tolerances:
    map_regression_max: float = 0.5  # points
    miou_regression_max: float = 0.5
    ece_max: float = 0.06  # absolute, not relative
    false_stop_ratio_max: float = 1.05  # vs. incumbent
    id_switch_ratio_max: float = 1.10
    min_scenarios: int = 1_500


# --- tier 1 ------------------------------------------------------------------


def tier1_aggregate(
    candidate: AggregateMetrics,
    incumbent: AggregateMetrics | None,
    tol: Tolerances,
) -> list[GateOutcome]:
    out: list[GateOutcome] = []

    # Calibration is an absolute bar, not a comparison. An overconfident model in
    # a whiteout is dangerous regardless of how the incumbent behaved.
    out.append(
        GateOutcome(
            gate="calibration_ece",
            tier=1,
            verdict=Verdict.PASS
            if candidate.expected_calibration_error <= tol.ece_max
            else Verdict.FAIL,
            detail="expected calibration error on the golden set",
            observed=candidate.expected_calibration_error,
            threshold=tol.ece_max,
        )
    )

    if incumbent is None:
        out.append(
            GateOutcome(
                gate="aggregate_baseline",
                tier=1,
                verdict=Verdict.PASS,
                detail="no incumbent; aggregate comparison skipped (first release)",
            )
        )
        return out

    for name, cand, inc, limit in (
        ("map_50_95", candidate.map_50_95, incumbent.map_50_95, tol.map_regression_max),
        ("miou", candidate.miou, incumbent.miou, tol.miou_regression_max),
    ):
        delta = cand - inc
        out.append(
            GateOutcome(
                gate=f"aggregate_{name}",
                tier=1,
                verdict=Verdict.PASS if delta >= -limit else Verdict.FAIL,
                detail=f"{name} delta vs incumbent: {delta:+.2f}",
                observed=delta,
                threshold=-limit,
            )
        )
    return out


# --- tier 2 ------------------------------------------------------------------


def tier2_slices(
    candidate: Mapping[str, float],
    incumbent: Mapping[str, float] | None,
) -> list[GateOutcome]:
    """Per-cell regression check. Tolerance comes from the taxonomy: zero headroom
    for safety-critical cells, 0.5 where personnel are present, 1.5 otherwise."""
    if incumbent is None:
        return [
            GateOutcome(
                gate="slice_baseline",
                tier=2,
                verdict=Verdict.PASS,
                detail="no incumbent; slice comparison skipped (first release)",
            )
        ]

    out: list[GateOutcome] = []
    for key, inc_value in incumbent.items():
        if key not in candidate:
            # A slice that vanished usually means the golden set lost coverage,
            # which is itself a problem worth blocking on.
            out.append(
                GateOutcome(
                    gate=f"slice_missing::{key}",
                    tier=2,
                    verdict=Verdict.FAIL,
                    detail="slice present in incumbent evaluation but absent here",
                )
            )
            continue

        cell = Cell.from_key(key)
        tol = slice_tolerance(cell)
        delta = candidate[key] - inc_value
        ok = delta >= -tol
        if not ok or cell.safety_critical:
            out.append(
                GateOutcome(
                    gate=f"slice::{key}",
                    tier=2,
                    verdict=Verdict.PASS if ok else Verdict.FAIL,
                    detail=(
                        f"{'SAFETY-CRITICAL ' if cell.safety_critical else ''}"
                        f"mAP delta {delta:+.2f} (tolerance {-tol:.2f})"
                    ),
                    observed=delta,
                    threshold=-tol,
                )
            )

    regressions = [o for o in out if o.blocking]
    if regressions:
        log.error("tier 2: %d slice regressions", len(regressions))
    return out or [
        GateOutcome(gate="slices", tier=2, verdict=Verdict.PASS, detail="no slice regressions")
    ]


# --- tier 3 ------------------------------------------------------------------


def tier3_closed_loop(
    candidate: ClosedLoopMetrics,
    incumbent: ClosedLoopMetrics | None,
    tol: Tolerances,
) -> list[GateOutcome]:
    out: list[GateOutcome] = []

    # Absolute, non-negotiable, no comparison to incumbent. One missed person
    # inside the safety envelope blocks the release outright.
    out.append(
        GateOutcome(
            gate="missed_personnel",
            tier=3,
            verdict=Verdict.PASS if candidate.missed_personnel_events == 0 else Verdict.FAIL,
            detail=("zero tolerance; failing frames are added to the golden set permanently"),
            observed=float(candidate.missed_personnel_events),
            threshold=0.0,
        )
    )

    # A replay suite that shrank is not evidence of anything.
    out.append(
        GateOutcome(
            gate="scenario_coverage",
            tier=3,
            verdict=Verdict.PASS if candidate.scenarios_run >= tol.min_scenarios else Verdict.FAIL,
            detail="closed-loop suite size",
            observed=float(candidate.scenarios_run),
            threshold=float(tol.min_scenarios),
        )
    )

    if incumbent is None:
        return out

    out.append(
        GateOutcome(
            gate="time_to_detect",
            tier=3,
            verdict=Verdict.PASS
            if candidate.time_to_detect_s <= incumbent.time_to_detect_s
            else Verdict.FAIL,
            detail=f"{candidate.time_to_detect_s:.3f}s vs incumbent {incumbent.time_to_detect_s:.3f}s",
            observed=candidate.time_to_detect_s,
            threshold=incumbent.time_to_detect_s,
        )
    )
    out.append(
        GateOutcome(
            gate="time_to_brake_margin",
            tier=3,
            verdict=Verdict.PASS
            if candidate.time_to_brake_margin_s >= incumbent.time_to_brake_margin_s
            else Verdict.FAIL,
            detail=f"{candidate.time_to_brake_margin_s:.3f}s vs incumbent {incumbent.time_to_brake_margin_s:.3f}s",
            observed=candidate.time_to_brake_margin_s,
            threshold=incumbent.time_to_brake_margin_s,
        )
    )

    # Nuisance stops are a real safety issue, not just an annoyance: operators
    # learn to override a model that cries wolf, and then it is not protecting
    # anyone.
    fs_limit = incumbent.false_stops_per_hour * tol.false_stop_ratio_max
    out.append(
        GateOutcome(
            gate="false_stops",
            tier=3,
            verdict=Verdict.PASS if candidate.false_stops_per_hour <= fs_limit else Verdict.FAIL,
            detail=f"{candidate.false_stops_per_hour:.3f}/h vs limit {fs_limit:.3f}/h",
            observed=candidate.false_stops_per_hour,
            threshold=fs_limit,
        )
    )

    id_limit = incumbent.id_switches_per_1k * tol.id_switch_ratio_max
    out.append(
        GateOutcome(
            gate="track_stability",
            tier=3,
            verdict=Verdict.PASS if candidate.id_switches_per_1k <= id_limit else Verdict.FAIL,
            detail=f"{candidate.id_switches_per_1k:.2f}/1k vs limit {id_limit:.2f}/1k",
            observed=candidate.id_switches_per_1k,
            threshold=id_limit,
        )
    )
    return out


# --- driver ------------------------------------------------------------------


def evaluate_all(
    result: EvaluationResult,
    incumbent_aggregate: AggregateMetrics | None = None,
    incumbent_slices: Mapping[str, float] | None = None,
    incumbent_closed_loop: ClosedLoopMetrics | None = None,
    tolerances: Tolerances | None = None,
    *,
    blocking: bool = True,
) -> EvaluationResult:
    """Run every gate and attach the outcomes.

    `blocking=False` in dev, where gates are advisory: the failures are still
    recorded, they just do not stop the pipeline. Everywhere else they block.
    """
    tol = tolerances or Tolerances()
    outcomes: list[GateOutcome] = []
    outcomes += tier1_aggregate(result.aggregate, incumbent_aggregate, tol)
    outcomes += tier2_slices(result.per_slice, incumbent_slices)
    outcomes += tier3_closed_loop(result.closed_loop, incumbent_closed_loop, tol)

    if not blocking:
        for o in outcomes:
            if o.verdict is Verdict.FAIL:
                o.verdict = Verdict.WAIVED
                o.detail += "  [waived: gates advisory in dev]"

    result.outcomes = outcomes

    failures = [o for o in outcomes if o.blocking]
    if failures:
        log.error(
            "GATE FAILURE for %s:%d -- %d blocking:\n%s",
            result.model,
            result.version,
            len(failures),
            "\n".join(f"  [t{o.tier}] {o.gate}: {o.detail}" for o in failures),
        )
    else:
        log.info("all gates passed for %s:%d", result.model, result.version)
    return result


__all__ = [
    "AggregateMetrics",
    "ClosedLoopMetrics",
    "EvaluationResult",
    "GateOutcome",
    "Tolerances",
    "Verdict",
    "evaluate_all",
    "tier1_aggregate",
    "tier2_slices",
    "tier3_closed_loop",
]
