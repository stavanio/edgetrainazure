"""On-robot frame triage.

Runs on the same Orin as perception, in the gap between inference frames. Decides
what to keep and in what order to upload it.

This module is why the whole pipeline is affordable. 40 robots x 180 GB/shift is
7.2 TB/day of raw sensor data against a shared site uplink that will never carry
it. Triage here cuts that ~85x, and -- more importantly -- what survives is
chosen for information content rather than sampled uniformly.

Every weight and threshold below is a **device-twin desired property**. Retuning
what the fleet finds interesting is a twin patch, not a software deployment. That
matters enormously the day a new failure mode appears and you want the whole
fleet hunting for it by end of shift.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import IntEnum

import numpy as np

log = logging.getLogger(__name__)


class Tier(IntEnum):
    """Retention tier. Lower value == higher priority. T0 is never shed."""

    EVENT = 0  # safety stop, disengagement, planner fault — permanent
    INTERESTING = 1  # scored above threshold — 7 days
    BACKGROUND = 2  # deterministic sample — 24 h
    DROP = 3


@dataclass(slots=True)
class ScoringWeights:
    """Twin-tunable. Defaults are the commissioning values."""

    entropy: float = 0.30
    ood: float = 0.30
    novelty: float = 0.25
    disagreement: float = 0.10
    safety_event: float = 1.00  # dominates by construction

    interesting_threshold: float = 0.45
    background_sample_period_s: float = 4.0

    # Above this the frame is not interesting, it is uninterpretable. Whiteouts
    # are cheap to generate and expensive to store, and they are rejected by the
    # cloud quality gate anyway — uploading them wastes the uplink twice.
    haze_reject_above: float = 0.62


@dataclass(slots=True)
class DeployedContext:
    """Shipped with each bundle. Refreshing the centroid every release is what
    stops the fleet re-collecting things the dataset already contains."""

    dataset_centroid: np.ndarray  # L2-normalised
    feature_mean: np.ndarray
    feature_precision: np.ndarray  # inverse covariance, for Mahalanobis
    eval_ood_rate: float = 0.0021  # reference rate; the fleet alert is 5x this


@dataclass(slots=True)
class FrameSignals:
    frame_id: str
    timestamp_ns: int
    logits: np.ndarray  # per-class, pre-softmax
    penultimate: np.ndarray  # backbone feature vector
    haze: float
    geometric_prior_disagrees: bool  # deployed model vs. cheap LiDAR-derived prior
    safety_event: bool = False


@dataclass(slots=True)
class Decision:
    frame_id: str
    tier: Tier
    priority: float
    components: dict[str, float] = field(default_factory=dict)


def predictive_entropy(logits: np.ndarray) -> float:
    """Normalised to [0, 1] so the weight has a stable meaning across heads."""
    z = logits - logits.max()
    p = np.exp(z)
    p = p / max(p.sum(), 1e-9)
    h = float(-(p * np.log(p + 1e-9)).sum())
    return h / float(np.log(len(p))) if len(p) > 1 else 0.0


def mahalanobis_ood(feature: np.ndarray, ctx: DeployedContext) -> float:
    """Squared Mahalanobis distance, squashed to [0, 1).

    Cheaper and more robust on-device than a learned OOD head, and it needs no
    extra parameters in the shipped engine — the statistics travel in the bundle.
    """
    d = feature - ctx.feature_mean
    m2 = float(d @ ctx.feature_precision @ d)
    return float(m2 / (m2 + 25.0))


def novelty(feature: np.ndarray, ctx: DeployedContext) -> float:
    """1 - cosine similarity to the deployed dataset centroid."""
    f = feature / max(float(np.linalg.norm(feature)), 1e-9)
    return float(np.clip(1.0 - f @ ctx.dataset_centroid, 0.0, 2.0) / 2.0)


class Curator:
    """Stateful triage over a frame stream."""

    def __init__(self, weights: ScoringWeights, ctx: DeployedContext) -> None:
        self.weights = weights
        self.ctx = ctx
        self._last_background_ns = 0

    def apply_twin_patch(self, desired: dict) -> None:
        """Apply a twin desired-properties patch.

        Unknown keys are ignored rather than raising: a cloud-side rollout may
        legitimately carry properties a not-yet-updated module does not know
        about, and refusing the whole patch over one of them would leave the
        robot on stale thresholds.
        """
        known = {f for f in ScoringWeights.__slots__}
        patch = {k: float(v) for k, v in desired.items() if k in known}
        unknown = set(desired) - known
        if unknown:
            log.info("ignoring unknown twin properties: %s", sorted(unknown))
        if patch:
            self.weights = replace(self.weights, **patch)
            log.info("applied twin patch: %s", patch)

    def score(self, s: FrameSignals) -> Decision:
        w = self.weights

        if s.safety_event:
            # T0 short-circuits everything. A safety event is retained with its
            # +/-30s window regardless of how ordinary the frame looks, because
            # the interesting part is often just outside the triggering frame.
            return Decision(s.frame_id, Tier.EVENT, 1.0, {"safety_event": 1.0})

        if s.haze > w.haze_reject_above:
            return Decision(s.frame_id, Tier.DROP, 0.0, {"haze": s.haze})

        components = {
            "entropy": predictive_entropy(s.logits),
            "ood": mahalanobis_ood(s.penultimate, self.ctx),
            "novelty": novelty(s.penultimate, self.ctx),
            "disagreement": 1.0 if s.geometric_prior_disagrees else 0.0,
        }
        priority = (
            w.entropy * components["entropy"]
            + w.ood * components["ood"]
            + w.novelty * components["novelty"]
            + w.disagreement * components["disagreement"]
        )

        if priority >= w.interesting_threshold:
            return Decision(s.frame_id, Tier.INTERESTING, priority, components)

        # Deterministic background sampling. Deterministic rather than random so
        # that two robots in the same drift do not sample the same instants, and
        # so the retained background set is reproducible.
        period_ns = int(w.background_sample_period_s * 1e9)
        if s.timestamp_ns - self._last_background_ns >= period_ns:
            self._last_background_ns = s.timestamp_ns
            return Decision(s.frame_id, Tier.BACKGROUND, priority, components)

        return Decision(s.frame_id, Tier.DROP, priority, components)


def upload_order(decisions: Sequence[Decision]) -> list[Decision]:
    """Tier first, then priority within tier.

    T0 is uploaded before anything else and blocks the queue until it is clear.
    Losing a safety event to a full ring buffer is the worst outcome this module
    can produce, so it is the case that gets no cleverness at all.
    """
    return sorted(
        (d for d in decisions if d.tier is not Tier.DROP),
        key=lambda d: (int(d.tier), -d.priority),
    )


def health_snapshot(decisions: Sequence[Decision], queue_depth: int, bundle: str) -> dict:
    """The `kind: drift_sketch` message routed to ADLS by IoT Hub.

    Deliberately a sketch, not raw predictions: it must be small enough to send
    every 30 s from a fleet, and the drift monitors only need distribution shape.
    """
    priorities = np.asarray([d.priority for d in decisions]) if decisions else np.zeros(1)
    tiers = {t.name.lower(): sum(1 for d in decisions if d.tier is t) for t in Tier}
    return {
        "kind": "drift_sketch",
        "bundle": bundle,
        "ts": int(time.time()),
        "frames_scored": len(decisions),
        "tiers": tiers,
        "priority_p50": float(np.percentile(priorities, 50)),
        "priority_p95": float(np.percentile(priorities, 95)),
        "queue_depth": queue_depth,
    }


__all__ = [
    "Curator",
    "Decision",
    "DeployedContext",
    "FrameSignals",
    "ScoringWeights",
    "Tier",
    "health_snapshot",
    "mahalanobis_ood",
    "novelty",
    "predictive_entropy",
    "upload_order",
]
