"""Teacher auto-labeling and triage routing.

Human labeling is ~40x the cost per frame of every compute step in this pipeline
combined (docs/07-cost-model.md). The goal is therefore not "label well" but
"label as few frames as possible, and only the ones that change the model."

The teacher is a large open-vocabulary detector plus a promptable segmenter,
served as an AML managed online endpoint on A100. It is slow, expensive per
frame, and never ships to a robot -- all of which is fine, because it runs
offline in batch.

Modeled routing mix, used to size the labeling budget. These are design
assumptions, not observations -- the auto-accept rate in particular has to be
established against a real teacher and a real dataset:

    auto-accept      ~71%   teacher confident, deployed student agrees
    human review     ~18%   teacher confident, student disagrees   <- highest value
    human label       ~9%   teacher unconfident
    redundant label   ~2%   safety-critical class involved
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

log = logging.getLogger(__name__)


class Route(StrEnum):
    AUTO_ACCEPT = "auto_accept"
    HUMAN_REVIEW = "human_review"  # correct a pre-label
    HUMAN_LABEL = "human_label"  # draw from scratch
    HUMAN_REDUNDANT = "human_redundant"  # 2x independent, adjudicated
    DISCARD = "discard"


SAFETY_CRITICAL_CLASSES = frozenset({"personnel", "personnel_prone", "hand_signal"})


@dataclass(slots=True)
class Detection:
    cls: str
    score: float
    box: tuple[float, float, float, float]  # xyxy, normalised
    mask_rle: str | None = None


@dataclass(slots=True)
class Prediction:
    frame_id: str
    detections: list[Detection] = field(default_factory=list)

    @property
    def max_score(self) -> float:
        return max((d.score for d in self.detections), default=0.0)

    @property
    def classes(self) -> set[str]:
        return {d.cls for d in self.detections}

    @property
    def touches_safety_class(self) -> bool:
        return bool(self.classes & SAFETY_CRITICAL_CLASSES)


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    teacher_confident_min: float = 0.72
    # Agreement is measured on the *set* of objects, not pixel-wise: what matters
    # for routing is whether the two models saw the same world, not whether their
    # boundaries match to a pixel.
    agreement_iou_min: float = 0.55
    agreement_count_slack: int = 0
    # Below this the teacher is not merely unconfident, it is uninformative --
    # a human drawing from scratch is faster than correcting noise.
    prelabel_useless_below: float = 0.25


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def agree(teacher: Prediction, student: Prediction, policy: RoutingPolicy) -> bool:
    """Do teacher and deployed student describe the same scene?

    Greedy matching within class. Deliberately strict on count: a student that
    finds three people where the teacher finds two is a disagreement worth a
    human's time even if the two it found match well.
    """
    for cls in teacher.classes | student.classes:
        t = [d for d in teacher.detections if d.cls == cls]
        s = [d for d in student.detections if d.cls == cls]
        if abs(len(t) - len(s)) > policy.agreement_count_slack:
            return False
        unmatched = list(s)
        for td in t:
            best, best_iou = None, 0.0
            for sd in unmatched:
                v = iou(td.box, sd.box)
                if v > best_iou:
                    best, best_iou = sd, v
            if best is None or best_iou < policy.agreement_iou_min:
                return False
            unmatched.remove(best)
    return True


def route(
    teacher: Prediction,
    student: Prediction | None,
    policy: RoutingPolicy | None = None,
) -> Route:
    """Decide who, if anyone, looks at this frame.

    Ordering is deliberate: the safety-class check comes first and overrides
    everything. A frame containing a person is worth two humans regardless of how
    confidently the models agree, because agreement between two models trained on
    the same data is not independent evidence.
    """
    policy = policy or RoutingPolicy()

    if teacher.touches_safety_class or (student and student.touches_safety_class):
        return Route.HUMAN_REDUNDANT

    confident = teacher.max_score >= policy.teacher_confident_min

    if confident and student is not None and agree(teacher, student, policy):
        return Route.AUTO_ACCEPT

    if confident:
        return Route.HUMAN_REVIEW

    if teacher.max_score < policy.prelabel_useless_below and not teacher.detections:
        # Genuinely empty frames are cheap to confirm and valuable as negatives.
        return Route.HUMAN_REVIEW

    return Route.HUMAN_LABEL


@dataclass(slots=True)
class RoutingStats:
    counts: dict[Route, int] = field(default_factory=lambda: {r: 0 for r in Route})

    def add(self, r: Route) -> None:
        self.counts[r] += 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def auto_accept_rate(self) -> float:
        return self.counts[Route.AUTO_ACCEPT] / self.total if self.total else 0.0

    def render(self) -> str:
        t = self.total or 1
        rows = [f"routed {self.total} frames:"]
        for r in Route:
            n = self.counts[r]
            if n:
                rows.append(f"  {r.value:<16} {n:>7}  {100.0 * n / t:5.1f}%")
        # Each point of auto-accept rate is roughly $350/month at the design point.
        rows.append(f"  auto-accept rate: {100.0 * self.auto_accept_rate:.1f}%")
        return "\n".join(rows)


def route_batch(
    teacher_preds: Sequence[Prediction],
    student_preds: dict[str, Prediction] | None = None,
    policy: RoutingPolicy | None = None,
) -> tuple[dict[str, Route], RoutingStats]:
    student_preds = student_preds or {}
    stats = RoutingStats()
    out: dict[str, Route] = {}
    for tp in teacher_preds:
        r = route(tp, student_preds.get(tp.frame_id), policy)
        out[tp.frame_id] = r
        stats.add(r)
    log.info("%s", stats.render())
    return out, stats


def label_confidence_weights(routes: dict[str, Route]) -> dict[str, float]:
    """Per-frame loss weights reflecting label provenance.

    Auto-accepted labels are correct most of the time but not always, and
    training on them at full weight lets teacher errors become student errors
    permanently. Down-weighting is cheap insurance.
    """
    weight = {
        Route.HUMAN_REDUNDANT: 1.0,
        Route.HUMAN_LABEL: 1.0,
        Route.HUMAN_REVIEW: 0.95,
        Route.AUTO_ACCEPT: 0.70,
        Route.DISCARD: 0.0,
    }
    return {fid: weight[r] for fid, r in routes.items()}


__all__ = [
    "SAFETY_CRITICAL_CLASSES",
    "Detection",
    "Prediction",
    "Route",
    "RoutingPolicy",
    "RoutingStats",
    "agree",
    "iou",
    "label_confidence_weights",
    "route",
    "route_batch",
]
