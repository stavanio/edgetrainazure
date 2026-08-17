"""SLI evaluation, error budgets, burn rate, and release posture.

Reads the catalogue in observability/slo.yaml and turns raw good/valid counts
into the three things anyone actually acts on:

  1. Are we meeting the objective?
  2. How much error budget is left, and how fast are we spending it?
  3. Given that, are we allowed to ship?

Question 3 is the one that matters. An error budget nobody enforces is a number
on a dashboard; `release_posture` makes it a rule.

Invariants are handled separately and deliberately do not flow through any of
the budget math -- see docs/06-sli-slo-and-telemetry.md §6.1. Expressing "no
missed personnel" as a percentage would make it negotiable, which it is not.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CATALOGUE = Path(__file__).resolve().parents[3] / "observability" / "slo.yaml"


class Kind(StrEnum):
    """How an indicator's objective is expressed."""

    FRACTION = "fraction"  # good/valid against a target floor
    RATIO_THRESHOLD = "ratio_threshold"  # numerator/denominator under a ceiling
    TREND = "trend"  # regression slope, direction not level


class Status(StrEnum):
    MEETING = "meeting"
    AT_RISK = "at_risk"  # meeting the objective, but burning budget fast
    BREACHING = "breaching"
    NO_DATA = "no_data"


class Posture(StrEnum):
    """What the error-budget policy permits right now."""

    SHIP = "ship"
    CONSTRAINED = "constrained"  # no discretionary edge-plane changes
    FREEZE = "freeze"


class Severity(StrEnum):
    PAGE = "page"
    TICKET = "ticket"


# --- catalogue ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BurnRateRule:
    severity: Severity
    burn_rate: float
    long_window: str
    short_window: str
    budget_consumed_pct: float


@dataclass(frozen=True, slots=True)
class Invariant:
    id: str
    name: str
    description: str
    breach_action: str
    scope: tuple[str, ...]

    @property
    def blocks_releases(self) -> bool:
        return self.breach_action == "block_release_fleetwide"


@dataclass(frozen=True, slots=True)
class Slo:
    id: str
    group: str
    name: str
    kind: Kind
    window: str
    query: str
    target: float | None = None  # FRACTION: floor
    target_max: float | None = None  # RATIO_THRESHOLD: ceiling
    target_slope_max: float | None = None  # TREND: max permitted slope
    rationale: str = ""

    @property
    def window_days(self) -> int:
        return _iso_days(self.window)


@dataclass(slots=True)
class Catalogue:
    version: int
    burn_rate_policy: list[BurnRateRule]
    invariants: list[Invariant]
    slos: list[Slo]
    healthy_above_pct: float
    constrained_above_pct: float
    dashboards: list[dict[str, Any]] = field(default_factory=list)

    def slo(self, slo_id: str) -> Slo:
        for s in self.slos:
            if s.id == slo_id:
                return s
        raise KeyError(f"unknown SLO {slo_id!r}")

    def by_group(self, group: str) -> list[Slo]:
        return [s for s in self.slos if s.group == group]


def _iso_days(window: str) -> int:
    """Minimal ISO-8601 duration parser for the day/hour/minute forms used here.

    Deliberately not a general parser: the catalogue only ever uses P28D, P90D,
    PT1H, PT6H, PT5M and friends, and a full implementation would be more code
    than the thing it supports.
    """
    if not window.startswith("P"):
        raise ValueError(f"not an ISO-8601 duration: {window!r}")
    body = window[1:]
    if body.startswith("T"):
        hours = _extract(body[1:], "H")
        minutes = _extract(body[1:], "M")
        return max(1, math.ceil((hours + minutes / 60) / 24))
    days = _extract(body.split("T")[0], "D")
    return int(days)


def _extract(text: str, unit: str) -> float:
    idx = text.find(unit)
    if idx < 0:
        return 0.0
    start = idx - 1
    while start >= 0 and (text[start].isdigit() or text[start] == "."):
        start -= 1
    return float(text[start + 1 : idx] or 0)


def load(path: Path | None = None) -> Catalogue:
    doc = yaml.safe_load((path or CATALOGUE).read_text())

    slos: list[Slo] = []
    for raw in doc["slos"]:
        kind = Kind(raw.get("kind", "fraction"))
        slos.append(
            Slo(
                id=raw["id"],
                group=raw["group"],
                name=raw["name"],
                kind=kind,
                window=raw["window"],
                query=raw.get("query", ""),
                target=raw.get("target"),
                target_max=raw.get("target_max"),
                target_slope_max=raw.get("target_slope_max"),
                rationale=raw.get("rationale", "").strip(),
            )
        )

    return Catalogue(
        version=doc["version"],
        burn_rate_policy=[
            BurnRateRule(
                severity=Severity(r["severity"]),
                burn_rate=float(r["burn_rate"]),
                long_window=r["long_window"],
                short_window=r["short_window"],
                budget_consumed_pct=float(r["budget_consumed_pct"]),
            )
            for r in doc["burn_rate_policy"]
        ],
        invariants=[
            Invariant(
                id=i["id"],
                name=i["name"],
                description=i["description"],
                breach_action=i["breach_action"],
                scope=tuple(i.get("scope", ())),
            )
            for i in doc["invariants"]
        ],
        slos=slos,
        healthy_above_pct=float(doc["budget_policy"]["healthy_above_pct"]),
        constrained_above_pct=float(doc["budget_policy"]["constrained_above_pct"]),
        dashboards=doc.get("dashboards", []),
    )


# --- observations and evaluation ---------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """Raw counts for one SLI over one window, straight from its KQL query."""

    slo_id: str
    good: float = 0.0
    valid: float = 0.0
    numerator: float = 0.0  # ratio_threshold / trend
    denominator: float = 0.0
    series: tuple[tuple[float, float], ...] = ()  # trend: (x_days, y_value)


@dataclass(slots=True)
class Evaluation:
    slo: Slo
    status: Status
    observed: float | None
    objective: float | None
    budget_remaining_pct: float | None
    burn_rate: float | None
    detail: str = ""

    @property
    def meeting(self) -> bool:
        return self.status in (Status.MEETING, Status.AT_RISK, Status.NO_DATA)


def evaluate(slo: Slo, obs: Observation) -> Evaluation:
    if slo.kind is Kind.FRACTION:
        return _evaluate_fraction(slo, obs)
    if slo.kind is Kind.RATIO_THRESHOLD:
        return _evaluate_ratio(slo, obs)
    return _evaluate_trend(slo, obs)


def _evaluate_fraction(slo: Slo, obs: Observation) -> Evaluation:
    if obs.valid <= 0:
        return Evaluation(
            slo, Status.NO_DATA, None, slo.target, None, None, "no valid events in window"
        )

    assert slo.target is not None
    observed = obs.good / obs.valid
    budget = 1.0 - slo.target
    # A target of exactly 1.0 has no budget by construction (C5, C6). Any failure
    # is 0% remaining -- which is the intended semantics, not a divide-by-zero.
    if budget <= 0:
        remaining = 100.0 if observed >= 1.0 else 0.0
        burn = 0.0 if observed >= 1.0 else math.inf
    else:
        consumed = (1.0 - observed) / budget
        remaining = max(0.0, 100.0 * (1.0 - consumed))
        burn = (1.0 - observed) / budget

    status = Status.MEETING if observed >= slo.target else Status.BREACHING
    if status is Status.MEETING and remaining < 25.0:
        status = Status.AT_RISK

    return Evaluation(
        slo,
        status,
        observed,
        slo.target,
        remaining,
        burn,
        f"{obs.good:,.0f}/{obs.valid:,.0f} good events over {slo.window}",
    )


def _evaluate_ratio(slo: Slo, obs: Observation) -> Evaluation:
    if obs.denominator <= 0:
        return Evaluation(
            slo, Status.NO_DATA, None, slo.target_max, None, None, "empty denominator"
        )

    assert slo.target_max is not None
    observed = obs.numerator / obs.denominator
    # "Budget" for a cost ceiling is headroom under it, expressed the same way so
    # the dashboards can render every indicator with one component.
    remaining = max(0.0, 100.0 * (1.0 - observed / slo.target_max))
    burn = observed / slo.target_max

    status = Status.MEETING if observed <= slo.target_max else Status.BREACHING
    if status is Status.MEETING and remaining < 15.0:
        status = Status.AT_RISK

    return Evaluation(
        slo,
        status,
        observed,
        slo.target_max,
        remaining,
        burn,
        f"{observed:,.4f} against ceiling {slo.target_max:,.2f}",
    )


def _evaluate_trend(slo: Slo, obs: Observation) -> Evaluation:
    """Least-squares slope over the window. The objective is a direction.

    D7 (cost per robot-month) is the case: the absolute number depends on fleet
    size, so a threshold says nothing. The claim being tested is that unit
    economics improve as the fleet grows, and that claim is a negative slope.
    """
    if len(obs.series) < 3:
        return Evaluation(
            slo,
            Status.NO_DATA,
            None,
            slo.target_slope_max,
            None,
            None,
            "need at least 3 points to fit a trend",
        )

    xs = [p[0] for p in obs.series]
    ys = [p[1] for p in obs.series]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return Evaluation(
            slo,
            Status.NO_DATA,
            None,
            slo.target_slope_max,
            None,
            None,
            "all observations at the same timestamp",
        )

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom
    ceiling = slo.target_slope_max if slo.target_slope_max is not None else 0.0
    status = Status.MEETING if slope <= ceiling else Status.BREACHING

    direction = "falling" if slope < 0 else "rising" if slope > 0 else "flat"
    return Evaluation(
        slo,
        status,
        slope,
        ceiling,
        None,
        None,
        f"{direction} {abs(slope):,.2f}/day over {slo.window} ({n} points)",
    )


# --- burn rate ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BurnAlert:
    slo_id: str
    severity: Severity
    rule_burn_rate: float
    long_window: str
    short_window: str
    observed_long: float
    observed_short: float


def evaluate_burn_rate(
    slo: Slo,
    policy: Sequence[BurnRateRule],
    *,
    long_window_sli: Mapping[str, float],
    short_window_sli: Mapping[str, float],
) -> list[BurnAlert]:
    """Multi-window, multi-burn-rate alerting.

    Both windows must be burning above the rule's rate. Requiring the short
    window is what stops an alert persisting long after the problem resolved --
    the long window alone stays hot for hours after recovery.

    Returns the alerts that fire, most severe first.
    """
    if slo.kind is not Kind.FRACTION or slo.target is None:
        return []
    budget = 1.0 - slo.target
    if budget <= 0:
        return []

    fired: list[BurnAlert] = []
    for rule in policy:
        long_burn = _burn(long_window_sli.get(rule.long_window), budget)
        short_burn = _burn(short_window_sli.get(rule.short_window), budget)
        if long_burn is None or short_burn is None:
            continue
        if long_burn >= rule.burn_rate and short_burn >= rule.burn_rate:
            fired.append(
                BurnAlert(
                    slo_id=slo.id,
                    severity=rule.severity,
                    rule_burn_rate=rule.burn_rate,
                    long_window=rule.long_window,
                    short_window=rule.short_window,
                    observed_long=long_burn,
                    observed_short=short_burn,
                )
            )

    fired.sort(key=lambda a: (a.severity is not Severity.PAGE, -a.rule_burn_rate))
    return fired


def _burn(sli_value: float | None, budget: float) -> float | None:
    if sli_value is None:
        return None
    return (1.0 - sli_value) / budget


# --- invariants and posture --------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvariantBreach:
    invariant: Invariant
    count: int
    detail: str


def check_invariants(
    catalogue: Catalogue, counts: Mapping[str, int], *, details: Mapping[str, str] | None = None
) -> list[InvariantBreach]:
    """Any non-zero count is a breach. There is no threshold to tune."""
    details = details or {}
    breaches = [
        InvariantBreach(inv, counts.get(inv.name, 0), details.get(inv.name, ""))
        for inv in catalogue.invariants
        if counts.get(inv.name, 0) > 0
    ]
    for b in breaches:
        log.critical(
            "INVARIANT BREACH %s (%s): %d occurrence(s) -- action: %s",
            b.invariant.id,
            b.invariant.name,
            b.count,
            b.invariant.breach_action,
        )
    return breaches


@dataclass(slots=True)
class PostureDecision:
    posture: Posture
    reason: str
    blocking_invariants: list[str] = field(default_factory=list)
    exhausted_slos: list[str] = field(default_factory=list)
    constrained_slos: list[str] = field(default_factory=list)


def release_posture(
    catalogue: Catalogue,
    evaluations: Iterable[Evaluation],
    breaches: Sequence[InvariantBreach] = (),
) -> PostureDecision:
    """Turn budget state into a shipping decision.

    Ordering matters and is deliberate:
      1. Any release-blocking invariant breach freezes everything, regardless of
         how healthy every budget looks.
      2. An exhausted budget freezes that plane.
      3. A low budget constrains it.

    This is the function that makes the error budget a rule rather than a chart.
    """
    blocking = [b.invariant.id for b in breaches if b.invariant.blocks_releases]
    if blocking:
        return PostureDecision(
            Posture.FREEZE,
            f"invariant breach: {', '.join(blocking)} -- releases blocked fleet-wide",
            blocking_invariants=blocking,
        )
    if breaches:
        # Non-blocking invariants (device quarantine, ring rollback) still stop
        # forward motion; they just do not freeze the whole fleet's pipeline.
        return PostureDecision(
            Posture.CONSTRAINED,
            f"invariant breach: {', '.join(b.invariant.id for b in breaches)}",
            blocking_invariants=[b.invariant.id for b in breaches],
        )

    exhausted: list[str] = []
    constrained: list[str] = []
    for ev in evaluations:
        if ev.budget_remaining_pct is None:
            if ev.status is Status.BREACHING:
                exhausted.append(ev.slo.id)
            continue
        if ev.budget_remaining_pct < catalogue.constrained_above_pct:
            exhausted.append(ev.slo.id)
        elif ev.budget_remaining_pct < catalogue.healthy_above_pct:
            constrained.append(ev.slo.id)

    if exhausted:
        return PostureDecision(
            Posture.FREEZE,
            f"error budget exhausted: {', '.join(sorted(exhausted))} -- "
            "reliability work takes priority over model improvements",
            exhausted_slos=sorted(exhausted),
            constrained_slos=sorted(constrained),
        )
    if constrained:
        return PostureDecision(
            Posture.CONSTRAINED,
            f"error budget low: {', '.join(sorted(constrained))} -- "
            "no discretionary edge-plane changes",
            constrained_slos=sorted(constrained),
        )
    return PostureDecision(Posture.SHIP, "all budgets healthy")


# --- reporting ---------------------------------------------------------------


def render_report(
    evaluations: Sequence[Evaluation],
    decision: PostureDecision,
    breaches: Sequence[InvariantBreach] = (),
) -> str:
    """Plain-text roll-up for `make slo-report` and the weekly review."""
    lines: list[str] = ["", "edgeforge SLO report", "=" * 60, ""]

    lines.append(f"RELEASE POSTURE: {decision.posture.value.upper()}")
    lines.append(f"  {decision.reason}")
    lines.append("")

    if breaches:
        lines.append("INVARIANT BREACHES")
        for b in breaches:
            lines.append(
                f"  [{b.invariant.id}] {b.invariant.name}: {b.count} -> {b.invariant.breach_action}"
            )
        lines.append("")

    by_group: dict[str, list[Evaluation]] = {}
    for ev in evaluations:
        by_group.setdefault(ev.slo.group, []).append(ev)

    for group in sorted(by_group):
        lines.append(f"{group.upper()}")
        lines.append(
            f"  {'ID':<4} {'INDICATOR':<30} {'OBSERVED':>11} {'OBJECTIVE':>11} "
            f"{'BUDGET':>8}  STATUS"
        )
        for ev in sorted(by_group[group], key=lambda e: e.slo.id):
            observed = "  --" if ev.observed is None else _fmt(ev.slo, ev.observed)
            objective = "  --" if ev.objective is None else _fmt(ev.slo, ev.objective)
            budget = (
                "  --" if ev.budget_remaining_pct is None else f"{ev.budget_remaining_pct:6.1f}%"
            )
            marker = {
                Status.MEETING: "ok",
                Status.AT_RISK: "AT RISK",
                Status.BREACHING: "BREACHING",
                Status.NO_DATA: "no data",
            }[ev.status]
            lines.append(
                f"  {ev.slo.id:<4} {ev.slo.name:<30} {observed:>11} {objective:>11} "
                f"{budget:>8}  {marker}"
            )
        lines.append("")

    return "\n".join(lines)


def _fmt(slo: Slo, value: float) -> str:
    if slo.kind is Kind.FRACTION:
        return f"{100 * value:.3f}%"
    if slo.kind is Kind.RATIO_THRESHOLD:
        return f"${value:,.2f}" if value < 1000 else f"${value:,.0f}"
    return f"{value:+,.2f}/d"


__all__ = [
    "CATALOGUE",
    "BurnAlert",
    "BurnRateRule",
    "Catalogue",
    "Evaluation",
    "Invariant",
    "InvariantBreach",
    "Kind",
    "Observation",
    "Posture",
    "PostureDecision",
    "Severity",
    "Slo",
    "Status",
    "check_invariants",
    "evaluate",
    "evaluate_burn_rate",
    "load",
    "release_posture",
    "render_report",
]
