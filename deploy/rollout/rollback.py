#!/usr/bin/env python3
"""Rollback driver.

Invoked three ways:
  - automatically, by the Azure Monitor rules in infra/observability.tf
  - by rollout.py when a soak window fails
  - by hand, from the Makefile, when an operator says it feels wrong

That third path matters. "It feels wrong" from someone who drives the machine
every shift is a valid reason and does not need justification at the time.

Rollback patches desired properties back to last-known-good. Because every robot
retains the previous bundle on disk, recovery does not require downloading
anything -- which is essential, because a robot underground has no network and
the whole point of a rollback is that it works when things are going badly.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("edgeforge.rollback")

RINGS_FILE = Path(__file__).with_name("rings.yaml")


@dataclass(slots=True)
class Predicate:
    id: str
    expr: str
    consecutive_windows: int
    severity: str


def load_predicates(path: Path = RINGS_FILE) -> list[Predicate]:
    doc = yaml.safe_load(path.read_text())
    return [
        Predicate(
            id=p["id"],
            expr=p["expr"],
            consecutive_windows=int(p.get("consecutive_windows", 1)),
            severity=p.get("severity", "medium"),
        )
        for p in doc.get("rollback_predicates", [])
    ]


def az(*args: str) -> str:
    proc = subprocess.run(["az", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"az failed: {' '.join(args)}\n{proc.stderr[-2000:]}")
    return proc.stdout


def fetch_health(hub: str, ring: str, release_id: str) -> list[dict]:
    """Recent health-contract samples for a ring, newest first."""
    query = f"""
    FleetHealth_CL
    | where TimeGenerated > ago(30m)
    | where tags_ring_s == '{ring}' and release_id_s == '{release_id}'
    | order by TimeGenerated desc
    """
    out = az(
        "monitor",
        "log-analytics",
        "query",
        "-w",
        _workspace_id(),
        "--analytics-query",
        query,
        "-o",
        "json",
    )
    return json.loads(out or "[]")


def _workspace_id() -> str:
    import os

    ws = os.environ.get("AZ_LOG_ANALYTICS_WORKSPACE_ID")
    if not ws:
        raise SystemExit("AZ_LOG_ANALYTICS_WORKSPACE_ID is not set")
    return ws


def evaluate_predicates(samples: list[dict]) -> list[str]:
    """Return the IDs of predicates that fired.

    Evaluated in plain Python rather than by interpreting the `expr` strings in
    rings.yaml: those strings are documentation for humans, and an eval-based
    implementation would turn a config file into executable code reachable from
    a rollout path. Keep the two in sync by hand -- there are seven of them.
    """
    if not samples:
        return []

    fired: list[str] = []
    predicates = {p.id: p for p in load_predicates()}

    def consecutive(key: str, test) -> int:
        n = 0
        for s in samples:  # newest first
            if key in s and test(s[key]):
                n += 1
            else:
                break
        return n

    if (
        consecutive("inference_p99_ms", lambda v: v > 45)
        >= predicates["latency_budget_exceeded"].consecutive_windows
    ):
        fired.append("latency_budget_exceeded")

    if (
        consecutive("dropped_frames_pct", lambda v: v > 0.5)
        >= predicates["frames_dropped"].consecutive_windows
    ):
        fired.append("frames_dropped")

    if (
        consecutive("throttled_pct", lambda v: v > 5)
        >= predicates["thermal_throttling"].consecutive_windows
    ):
        fired.append("thermal_throttling")

    # Single-sample criticals: one is enough.
    if any(s.get("safety_envelope_violations", 0) > 0 for s in samples):
        fired.append("safety_envelope_violation")
    if any(s.get("disengagements_attributed_to_perception", 0) > 0 for s in samples):
        fired.append("perception_disengagement")

    baseline_mu = samples[0].get("ring_baseline_personnel_mu")
    baseline_sigma = samples[0].get("ring_baseline_personnel_sigma")
    if baseline_mu is not None and baseline_sigma:
        deviated = consecutive(
            "detections_personnel_per_km",
            lambda v: abs(v - baseline_mu) > 3 * baseline_sigma,
        )
        if deviated >= predicates["personnel_rate_deviation"].consecutive_windows:
            fired.append("personnel_rate_deviation")

    return fired


def last_known_good(hub: str, ring: str) -> str:
    """Read the reported last-known-good bundle from the ring's devices.

    Taken from *reported* properties, not from a cloud-side record: the reported
    value is what a device actually has on disk and can load without a network.
    A cloud-side record can disagree, and preferring it is how a rollback turns
    into a device stuck waiting to download something underground.
    """
    twins = json.loads(
        az(
            "iot",
            "hub",
            "device-twin",
            "list",
            "-n",
            hub,
            "--query",
            f"[?tags.ring=='{ring}'].properties.reported.perception",
            "-o",
            "json",
        )
        or "[]"
    )
    candidates = {t.get("last_known_good") for t in twins if t and t.get("last_known_good")}
    if not candidates:
        raise SystemExit(
            f"no device in ring {ring} reports a last-known-good bundle; "
            "manual intervention required"
        )
    if len(candidates) > 1:
        # Pick the one the most devices hold, so the rollback needs no downloads.
        counts: dict[str, int] = {}
        for t in twins:
            lkg = (t or {}).get("last_known_good")
            if lkg:
                counts[lkg] = counts.get(lkg, 0) + 1
        chosen = max(counts, key=counts.get)  # type: ignore[arg-type]
        log.warning("ring %s has mixed last-known-good %s; choosing %s", ring, counts, chosen)
        return chosen
    return candidates.pop()


def rollback(hub: str, ring: str, bundle: str | None = None, *, reason: str = "manual") -> int:
    target = bundle or last_known_good(hub, ring)
    devices = json.loads(
        az(
            "iot",
            "hub",
            "device-twin",
            "list",
            "-n",
            hub,
            "--query",
            f"[?tags.ring=='{ring}'].deviceId",
            "-o",
            "json",
        )
        or "[]"
    )
    if not devices:
        log.warning("ring %s matched no devices", ring)
        return 0

    patch = {
        "properties": {
            "desired": {
                "perception": {
                    "bundle": target,
                    "shadow_mode": False,
                    "rollback_reason": reason,
                }
            }
        }
    }

    log.error(
        "ROLLING BACK ring %s to %s across %d device(s); reason=%s",
        ring,
        target,
        len(devices),
        reason,
    )
    for device_id in devices:
        az(
            "iot",
            "hub",
            "device-twin",
            "update",
            "-n",
            hub,
            "-d",
            device_id,
            "--set",
            json.dumps(patch),
        )
    log.info("rollback patched; devices converge on twin propagation")
    return len(devices)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="edgeforge rollback")
    p.add_argument("--hub", required=True)
    p.add_argument("--ring", required=True)
    p.add_argument("--bundle", help="specific bundle; defaults to last-known-good")
    p.add_argument("--reason", default="manual")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rollback(a.hub, a.ring, a.bundle, reason=a.reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
