"""Dedupe and stratified sampling: /clean -> /curated.

Two stages, in this order:

  1. Near-duplicate removal. ~60-70% of quality-passed field frames are
     near-copies of frames already in the dataset. A stationary robot emits
     hundreds of identical frames; the same drift traversed twice yields
     near-identical sequences at different speeds.

  2. Stratification against the scenario taxonomy. This is the step naive
     pipelines skip, and it is why they fail. Uniform sampling produces a
     dataset whose distribution matches the fleet's -- and the fleet mostly
     drives down empty, well-lit main drifts. The model that results is
     excellent at the common case and dangerous in the rare one.

Labeling budget is the scarcest resource in the system (docs/07-cost-model.md);
everything here exists to spend it well.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from edgeforge.taxonomy import CELL_SHARE_CEILING, Cell, sampling_floor

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Frame:
    frame_id: str
    cell: Cell
    phash: int  # 64-bit perceptual hash
    embedding: np.ndarray  # L2-normalised backbone embedding
    site: str
    robot: str
    timestamp_ns: int
    priority: float  # curator score from the robot; higher == more interesting
    reason: str  # novel | uncertain | disagreement | safety-event | background


# --- stage 1: near-duplicate removal ----------------------------------------


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedupe(
    frames: Sequence[Frame],
    *,
    phash_max_distance: int = 6,
    embedding_min_distance: float = 0.08,
    temporal_window_ns: int = 20_000_000_000,  # 20 s
) -> list[Frame]:
    """Two-stage dedupe.

    Stage A (phash, cheap) catches exact and near-exact repeats within a
    temporal window on the same robot -- the stationary-robot case.

    Stage B (embedding cosine) catches semantic duplicates across time and
    robots -- the same drift traversed twice. This is the expensive one, so it
    only runs on what survives stage A.

    Within a duplicate group the highest-priority frame is kept, not the first.
    The robot already told us which frame it found most interesting; discarding
    that judgement to keep an arbitrary representative wastes the on-robot
    scoring entirely.
    """
    if not frames:
        return []

    ordered = sorted(frames, key=lambda f: (f.robot, f.timestamp_ns))

    # --- Stage A: temporal phash grouping, per robot
    groups: list[list[Frame]] = []
    current: list[Frame] = [ordered[0]]
    for f in ordered[1:]:
        anchor = current[0]
        same_robot = f.robot == anchor.robot
        in_window = abs(f.timestamp_ns - anchor.timestamp_ns) <= temporal_window_ns
        similar = hamming(f.phash, anchor.phash) <= phash_max_distance
        if same_robot and in_window and similar:
            current.append(f)
        else:
            groups.append(current)
            current = [f]
    groups.append(current)

    stage_a = [max(g, key=lambda f: f.priority) for g in groups]
    log.info("dedupe stage A (phash): %d -> %d frames", len(frames), len(stage_a))

    # --- Stage B: greedy embedding-space thinning, highest priority first
    kept: list[Frame] = []
    kept_embeddings: list[np.ndarray] = []
    for f in sorted(stage_a, key=lambda x: -x.priority):
        if kept_embeddings:
            sims = np.asarray(kept_embeddings) @ f.embedding
            if float(1.0 - sims.max()) < embedding_min_distance:
                continue
        kept.append(f)
        kept_embeddings.append(f.embedding)

    log.info("dedupe stage B (embedding): %d -> %d frames", len(stage_a), len(kept))
    return kept


# --- stage 2: stratified sampling -------------------------------------------


@dataclass(slots=True)
class StratificationReport:
    """Emitted alongside every snapshot. Read it before launching training.

    A cell below its floor at snapshot time becomes a failed slice gate at day 6
    of an 11-day loop -- a wasted cycle for a shortfall that was visible on day 4.
    """

    selected: int
    per_cell: dict[str, int]
    below_floor: dict[str, tuple[int, int]]  # cell -> (have, need)
    capped: dict[str, tuple[int, int]]  # cell -> (available, taken)
    safety_critical_shortfall: dict[str, tuple[int, int]]

    @property
    def ok(self) -> bool:
        """Safety-critical shortfalls block; ordinary shortfalls warn."""
        return not self.safety_critical_shortfall

    def render(self) -> str:
        lines = [f"stratification: {self.selected} frames selected"]
        if self.safety_critical_shortfall:
            lines.append("  BLOCKING -- safety-critical cells below floor:")
            for k, (have, need) in sorted(self.safety_critical_shortfall.items()):
                lines.append(f"    {k}: {have}/{need}")
        if self.below_floor:
            lines.append(f"  {len(self.below_floor)} cells below floor (non-blocking):")
            for k, (have, need) in sorted(self.below_floor.items())[:10]:
                lines.append(f"    {k}: {have}/{need}")
        if self.capped:
            lines.append(f"  {len(self.capped)} cells capped at the share ceiling")
        return "\n".join(lines)


def stratified_sample(
    frames: Iterable[Frame],
    *,
    target_size: int,
    share_ceiling: float = CELL_SHARE_CEILING,
    rng: np.random.Generator | None = None,
) -> tuple[list[Frame], StratificationReport]:
    """Select `target_size` frames honouring per-cell floors and a share ceiling.

    Order of operations matters:
      1. Satisfy floors first, taking the highest-priority frames per cell.
      2. Cap any cell at `share_ceiling` of the target.
      3. Fill the remainder proportionally to remaining availability.

    Doing (1) before (3) is what keeps rare safety-critical cells in the set.
    """
    rng = rng or np.random.default_rng(0xED6E)
    by_cell: dict[Cell, list[Frame]] = defaultdict(list)
    for f in frames:
        if f.cell.uninterpretable:
            continue
        by_cell[f.cell].append(f)

    for pool in by_cell.values():
        pool.sort(key=lambda f: -f.priority)

    ceiling = max(1, math.floor(share_ceiling * target_size))
    selected: list[Frame] = []
    taken: dict[Cell, int] = defaultdict(int)
    below_floor: dict[str, tuple[int, int]] = {}
    safety_shortfall: dict[str, tuple[int, int]] = {}
    capped: dict[str, tuple[int, int]] = {}

    # 1. floors
    for cell, pool in by_cell.items():
        floor = sampling_floor(cell)
        take = min(floor, ceiling, len(pool))
        selected.extend(pool[:take])
        taken[cell] = take

        # "Below floor" means the lake could not supply the frames -- go collect
        # more. "Capped" means the ceiling held back frames we already have.
        # Conflating the two sends an operator hunting for data that is already
        # sitting in /curated, so they are reported separately.
        if len(pool) < floor:
            record = (len(pool), floor)
            below_floor[cell.key] = record
            if cell.safety_critical:
                safety_shortfall[cell.key] = record
        elif take == ceiling < min(floor, len(pool)):
            capped[cell.key] = (len(pool), ceiling)

    # 2 + 3. proportional fill of whatever budget remains, respecting the ceiling
    remaining = target_size - len(selected)
    if remaining > 0:
        available = {
            cell: min(len(pool) - taken[cell], ceiling - taken[cell])
            for cell, pool in by_cell.items()
        }
        available = {c: n for c, n in available.items() if n > 0}
        total_available = sum(available.values())
        if total_available:
            for cell, avail in available.items():
                share = avail / total_available
                extra = min(avail, round(share * remaining))
                pool = by_cell[cell]
                selected.extend(pool[taken[cell] : taken[cell] + extra])
                if taken[cell] + extra >= ceiling and len(pool) > ceiling:
                    capped[cell.key] = (len(pool), ceiling)
                taken[cell] += extra

    rng.shuffle(selected)  # decorrelate cell order for the labeling queue

    report = StratificationReport(
        selected=len(selected),
        per_cell={c.key: n for c, n in taken.items() if n},
        below_floor=below_floor,
        capped=capped,
        safety_critical_shortfall=safety_shortfall,
    )
    log.info("%s", report.render())
    return selected, report


__all__ = ["Frame", "StratificationReport", "dedupe", "hamming", "stratified_sample"]
