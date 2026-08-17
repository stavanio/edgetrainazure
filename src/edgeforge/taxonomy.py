"""Scenario taxonomy.

Every frame in the lake carries a taxonomy cell. The taxonomy is the backbone of
three separate mechanisms:

  1. Stratified sampling in curation, so rare-but-critical conditions survive
     into the training set instead of being sampled out for being rare.
  2. Slice gates in evaluation, so a model cannot be promoted by improving on
     easy conditions while regressing on hard ones.
  3. Calibration-set construction for INT8 quantization, so quantization error
     does not concentrate exactly where accuracy matters most.

Adding a dimension is a breaking change: it invalidates existing slice baselines
and every stratification floor. Adding a *value* to an existing dimension is
additive and safe. See docs/08-runbook.md §8.5 on silent taxonomy drift.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from itertools import product


class Illumination(StrEnum):
    DARK = "dark"  # headlamps only, < 5 lux ambient
    LOW = "low"  # sparse fixed lighting
    MIXED = "mixed"  # strong gradients, glare sources in frame
    BRIGHT = "bright"  # well-lit workshop, portal, or surface


class Particulate(StrEnum):
    CLEAR = "clear"
    LIGHT = "light"
    HEAVY = "heavy"  # visibility materially reduced
    WHITEOUT = "whiteout"  # a human operator could not interpret the frame


class Surface(StrEnum):
    DRY_COMPACT = "dry_compact"
    LOOSE = "loose"  # muck pile, spillage, fresh blast
    WET = "wet"
    STANDING_WATER = "standing_water"
    ICE = "ice"


class Geometry(StrEnum):
    DRIFT = "drift"  # straight run
    JUNCTION = "junction"
    DECLINE = "decline"  # non-trivial grade
    CHAMBER = "chamber"  # large open volume, sparse returns
    CONFINED = "confined"  # tight clearance both sides


class Actors(StrEnum):
    NONE = "none"
    MACHINE = "machine"
    PERSONNEL = "personnel"
    BOTH = "both"


DIMENSIONS: dict[str, type[StrEnum]] = {
    "illumination": Illumination,
    "particulate": Particulate,
    "surface": Surface,
    "geometry": Geometry,
    "actors": Actors,
}


@dataclass(frozen=True, slots=True)
class Cell:
    """One taxonomy cell. Hashable, so it works as a dict key and a slice name."""

    illumination: Illumination
    particulate: Particulate
    surface: Surface
    geometry: Geometry
    actors: Actors

    @property
    def key(self) -> str:
        return "|".join(
            (
                self.illumination,
                self.particulate,
                self.surface,
                self.geometry,
                self.actors,
            )
        )

    @classmethod
    def from_key(cls, key: str) -> Cell:
        illum, part, surf, geom, act = key.split("|")
        return cls(
            Illumination(illum),
            Particulate(part),
            Surface(surf),
            Geometry(geom),
            Actors(act),
        )

    @property
    def personnel_present(self) -> bool:
        return self.actors in (Actors.PERSONNEL, Actors.BOTH)

    @property
    def safety_critical(self) -> bool:
        """Cells where a perception failure can injure someone.

        Personnel present in degraded visibility, or personnel on a grade where
        stopping distance is longest. These carry the highest sampling floors and
        the strictest slice gates.
        """
        if not self.personnel_present:
            return False
        return (
            self.particulate in (Particulate.HEAVY, Particulate.WHITEOUT)
            or self.illumination in (Illumination.DARK, Illumination.LOW)
            or self.geometry is Geometry.DECLINE
            or self.surface in (Surface.WET, Surface.STANDING_WATER, Surface.ICE)
        )

    @property
    def uninterpretable(self) -> bool:
        """Frames no human could label. Rejected at the quality gate, not trained on.

        A pipeline that trains on whiteouts teaches the model to be confident in
        conditions where confidence is not warranted.
        """
        return self.particulate is Particulate.WHITEOUT


def all_cells() -> Iterator[Cell]:
    """Every combination. 4 x 4 x 5 x 5 x 4 = 1,600 cells, ~340 of them realistic."""
    for combo in product(*(list(e) for e in DIMENSIONS.values())):
        yield Cell(*combo)  # type: ignore[arg-type]


def trainable_cells() -> Iterator[Cell]:
    yield from (c for c in all_cells() if not c.uninterpretable)


# Minimum frames per cell in a training snapshot. Cells below their floor are
# reported by `curation.snapshot` and are the single most common reason a
# training cycle is wasted -- the slice gate fails at day 6 for a shortfall that
# was visible at day 4.
def sampling_floor(cell: Cell) -> int:
    if cell.uninterpretable:
        return 0
    if cell.safety_critical:
        return 4_000
    if cell.personnel_present:
        return 1_500
    if cell.particulate in (Particulate.HEAVY,):
        return 800
    return 250


# Maximum share of a snapshot any single cell may occupy. Without this, the
# fleet's own distribution dominates -- and the fleet mostly drives down empty,
# well-lit main drifts.
CELL_SHARE_CEILING = 0.06


# Slice-gate tolerance: how far a cell's metric may regress against the incumbent
# regardless of aggregate movement. Safety-critical cells get no headroom at all.
def slice_tolerance(cell: Cell) -> float:
    if cell.safety_critical:
        return 0.0
    if cell.personnel_present:
        return 0.5
    return 1.5
