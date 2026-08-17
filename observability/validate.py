#!/usr/bin/env python3
"""Validate the observability definitions. Run by CI and by `make lint`.

slo.yaml is the single source of truth for three consumers: the Grafana
dashboards, the Azure Monitor rules generated in infra/observability.tf, and
slo.py. A dangling query reference or a dashboard listing an indicator that no
longer exists must fail here rather than at terraform apply time, or at 3am.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DASHBOARDS = ROOT / "dashboards"

VALID_SEVERITIES = {"low", "medium", "high", "critical"}
VALID_GROUPS = {"fleet", "pipeline", "efficiency"}
VALID_KINDS = {"fraction", "ratio_threshold", "trend"}


def check(problems: list[str], condition: bool, message: str) -> None:
    if not condition:
        problems.append(message)


def validate() -> list[str]:
    problems: list[str] = []
    doc = yaml.safe_load((ROOT / "slo.yaml").read_text())

    slos = doc["slos"]
    invariants = doc["invariants"]
    ids = [s["id"] for s in slos] + [i["id"] for i in invariants]

    check(problems, len(ids) == len(set(ids)), "duplicate indicator id in slo.yaml")

    for slo in slos:
        sid = slo["id"]
        kind = slo.get("kind", "fraction")
        check(problems, kind in VALID_KINDS, f"{sid}: unknown kind {kind!r}")
        check(problems, slo["group"] in VALID_GROUPS, f"{sid}: unknown group {slo['group']!r}")
        check(problems, bool(slo.get("query")), f"{sid}: no query defined")

        if slo.get("query"):
            check(
                problems,
                (ROOT / slo["query"]).exists(),
                f"{sid}: query file {slo['query']} does not exist",
            )

        # Every indicator must state how its objective is expressed. An SLO with
        # no target is a metric, not an objective.
        if kind == "fraction":
            target = slo.get("target")
            check(problems, target is not None, f"{sid}: fraction SLO has no target")
            if target is not None:
                check(problems, 0 < target <= 1.0, f"{sid}: target {target} outside (0, 1]")
        elif kind == "ratio_threshold":
            check(problems, slo.get("target_max") is not None, f"{sid}: no target_max")
        else:
            check(problems, slo.get("target_slope_max") is not None, f"{sid}: no target_slope_max")

    # Invariants must never acquire a target -- that is the whole distinction.
    for inv in invariants:
        check(
            problems,
            "target" not in inv,
            f"{inv['id']}: invariants must not have a target; "
            "expressing a safety invariant as a percentage makes it negotiable",
        )
        check(problems, bool(inv.get("breach_action")), f"{inv['id']}: no breach_action")

    known = set(ids)
    for dash in doc["dashboards"]:
        unknown = set(dash["slis"]) - known
        check(problems, not unknown, f"dashboard {dash['id']}: unknown SLIs {sorted(unknown)}")

        path = DASHBOARDS / f"{dash['id']}.json"
        check(problems, path.exists(), f"dashboard {dash['id']}: {path.name} missing")
        if path.exists():
            try:
                panel_doc = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                problems.append(f"dashboard {dash['id']}: invalid JSON -- {exc}")
                continue

            check(
                problems,
                panel_doc.get("editable") is False,
                f"dashboard {dash['id']}: must be editable=false -- "
                "a dashboard edited in the UI drifts from its definition and "
                "nobody can review it",
            )
            # Two y-scales on one panel is the most misleading thing a cost
            # dashboard can do. Caught here rather than in review.
            if '"axisPlacement": "right"' in path.read_text():
                problems.append(
                    f"dashboard {dash['id']}: dual-axis panel found; index to a "
                    "common base or split the panel"
                )

    # Burn-rate policy sanity: page tiers must burn faster than ticket tiers, or
    # the escalation ordering is inverted and the fast case files a work item.
    policy = doc["burn_rate_policy"]
    pages = [r["burn_rate"] for r in policy if r["severity"] == "page"]
    tickets = [r["burn_rate"] for r in policy if r["severity"] == "ticket"]
    check(problems, bool(pages) and bool(tickets), "burn_rate_policy needs both tiers")
    if pages and tickets:
        check(
            problems,
            min(pages) > max(tickets),
            "burn_rate_policy: page tiers must burn faster than every ticket tier",
        )

    budget = doc["budget_policy"]
    check(
        problems,
        budget["healthy_above_pct"] > budget["constrained_above_pct"],
        "budget_policy: healthy threshold must exceed the constrained threshold",
    )

    if not problems:
        print(
            f"observability ok: {len(slos)} SLOs, {len(invariants)} invariants, "
            f"{len(doc['dashboards'])} dashboards, "
            f"{len(policy)} burn-rate rules"
        )
    return problems


def main() -> int:
    problems = validate()
    for p in problems:
        print(f"::error::{p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
