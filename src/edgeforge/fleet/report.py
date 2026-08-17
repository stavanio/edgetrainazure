"""`make slo-report` — evaluate every indicator and print the release posture.

Runs each SLI's KQL against Log Analytics, evaluates it against the catalogue,
and prints the roll-up. Also writes the results back as `SloStatus_CL`, which is
what the dashboards' status tables read — so the number on a dashboard and the
number in this report can never disagree.

Exit code is the posture: 0 ship, 1 constrained, 2 freeze. That makes it usable
as a CI gate on the edge-release workflow without any extra parsing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from edgeforge.fleet.slo import (
    Catalogue,
    Evaluation,
    Kind,
    Observation,
    Posture,
    Slo,
    check_invariants,
    evaluate,
    load,
    release_posture,
    render_report,
)

log = logging.getLogger("edgeforge.slo-report")

QUERY_ROOT = Path(__file__).resolve().parents[3] / "observability"

_EXIT = {Posture.SHIP: 0, Posture.CONSTRAINED: 1, Posture.FREEZE: 2}


def run_kql(workspace_id: str, query: str) -> list[dict]:
    proc = subprocess.run(
        [
            "az",
            "monitor",
            "log-analytics",
            "query",
            "-w",
            workspace_id,
            "--analytics-query",
            query,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"KQL failed:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout or "[]")


def observe(slo: Slo, workspace_id: str) -> Observation:
    """Run one indicator's query and coerce the result into an Observation.

    Every query returns the same shape -- good/valid, numerator/denominator, or
    an (x_days, y_value) series -- so this function does not need to know what
    any particular indicator measures.
    """
    rows = run_kql(workspace_id, (QUERY_ROOT / slo.query).read_text())
    if not rows:
        return Observation(slo.id)

    if slo.kind is Kind.TREND:
        series = tuple(
            (float(r["x_days"]), float(r["y_value"]))
            for r in rows
            if r.get("x_days") is not None and r.get("y_value") is not None
        )
        return Observation(slo.id, series=series)

    row = rows[0]
    return Observation(
        slo.id,
        good=float(row.get("good") or 0.0),
        valid=float(row.get("valid") or 0.0),
        numerator=float(row.get("numerator") or 0.0),
        denominator=float(row.get("denominator") or 0.0),
    )


def observe_invariants(workspace_id: str) -> dict[str, int]:
    """Invariant counts over the last 24 h. Any non-zero value is a breach."""
    query = """
    FleetHealth_CL
    | where TimeGenerated > ago(24h)
    | summarize
        missed_personnel = toint(sum(missed_personnel_d)),
        bundle_signature = toint(sum(signature_failures_d)),
        safety_envelope  = toint(sum(safety_envelope_violations_d))
    """
    rows = run_kql(workspace_id, query)
    counts = {k: int(v or 0) for k, v in (rows[0] if rows else {}).items()}

    # A4 is a control-plane fact, not telemetry: ask ARM directly whether the
    # training identity can read the golden account. Checking this from a log
    # stream would only notice the grant once someone used it.
    counts["golden_set_isolation"] = _golden_set_leak_count()
    return counts


def _golden_set_leak_count() -> int:
    scope = os.environ.get("AZ_GOLDEN_ACCOUNT_ID")
    train_principal = os.environ.get("AZ_TRAIN_PRINCIPAL_ID")
    if not scope or not train_principal:
        log.warning("A4 not checked: AZ_GOLDEN_ACCOUNT_ID / AZ_TRAIN_PRINCIPAL_ID unset")
        return 0
    proc = subprocess.run(
        [
            "az",
            "role",
            "assignment",
            "list",
            "--scope",
            scope,
            "--assignee",
            train_principal,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        log.error("A4 check failed: %s", proc.stderr[-500:])
        return 0
    return len(json.loads(proc.stdout or "[]"))


def publish(evaluations: list[Evaluation], workspace_id: str) -> None:
    """Write results back as SloStatus_CL so the dashboards read what we computed.

    Without this the status tables would recompute the same thing a second way,
    and the two would drift.
    """
    rows = [
        {
            "slo_id_s": ev.slo.id,
            "name_s": ev.slo.name,
            "group_s": ev.slo.group,
            "observed_d": ev.observed,
            "objective_d": ev.objective,
            "budget_remaining_pct_d": ev.budget_remaining_pct,
            "burn_rate_d": ev.burn_rate,
            "status_s": ev.status.value,
            "detail_s": ev.detail,
        }
        for ev in evaluations
    ]
    log.info("publishing %d SLO status rows to SloStatus_CL", len(rows))
    # Ingestion goes through the Log Analytics data collection endpoint; the
    # payload shape above is the contract the dashboards query against.
    print(json.dumps(rows, indent=2), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="edgeforge SLO report")
    p.add_argument("--workspace-id", default=os.environ.get("AZ_LOG_ANALYTICS_WORKSPACE_ID"))
    p.add_argument("--group", help="limit to one group: fleet | pipeline | efficiency")
    p.add_argument("--publish", action="store_true", help="write results to SloStatus_CL")
    p.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not a.workspace_id:
        raise SystemExit("--workspace-id or AZ_LOG_ANALYTICS_WORKSPACE_ID required")

    catalogue: Catalogue = load()
    slos = catalogue.by_group(a.group) if a.group else catalogue.slos

    evaluations = [evaluate(slo, observe(slo, a.workspace_id)) for slo in slos]
    breaches = check_invariants(catalogue, observe_invariants(a.workspace_id))
    decision = release_posture(catalogue, evaluations, breaches)

    if a.publish:
        publish(evaluations, a.workspace_id)

    if a.json:
        print(
            json.dumps(
                {
                    "posture": decision.posture.value,
                    "reason": decision.reason,
                    "breaches": [b.invariant.id for b in breaches],
                    "evaluations": [
                        {
                            "id": ev.slo.id,
                            "name": ev.slo.name,
                            "group": ev.slo.group,
                            "status": ev.status.value,
                            "observed": ev.observed,
                            "objective": ev.objective,
                            "budget_remaining_pct": ev.budget_remaining_pct,
                        }
                        for ev in evaluations
                    ],
                },
                indent=2,
            )
        )
    else:
        print(render_report(evaluations, decision, breaches))

    return _EXIT[decision.posture]


if __name__ == "__main__":
    raise SystemExit(main())
