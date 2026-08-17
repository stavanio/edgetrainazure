#!/usr/bin/env python3
"""Ring rollout driver.

Verifies a bundle's signature, patches the device twins for a ring, and watches
fleet health through the soak window.

Twin patching rather than imperative jobs is the whole design. Desired state is
convergent: a robot that was underground during a rollout -- or during a
rollback -- arrives at the correct state when it reconnects, with no replay and
no per-device bookkeeping.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("edgeforge.rollout")

RINGS_FILE = Path(__file__).with_name("rings.yaml")
LOCK_TWIN_TAG = "edgeforge_rollout_lock"


@dataclass(slots=True)
class Ring:
    name: str
    target_condition: str
    shadow_mode: bool
    auto_advance: bool
    soak_shifts: int
    soak_duration: str | None


def load_rings(path: Path = RINGS_FILE) -> dict[str, Ring]:
    doc = yaml.safe_load(path.read_text())
    out: dict[str, Ring] = {}
    for r in doc["rings"]:
        soak = r.get("soak", {})
        out[r["name"]] = Ring(
            name=r["name"],
            target_condition=r["target_condition"],
            shadow_mode=bool(r.get("shadow_mode", False)),
            auto_advance=bool(r.get("auto_advance", False)),
            soak_shifts=int(soak.get("shifts", 0)),
            soak_duration=soak.get("duration"),
        )
    return out


def az(*args: str, capture: bool = True) -> str:
    """Run an `az` command with the rollout managed identity.

    Shelling out to the CLI rather than using the SDK keeps every action in this
    script reproducible by hand during an incident, which matters more here than
    elegance.
    """
    cmd = ["az", *args]
    log.debug("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=capture, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"az failed: {' '.join(cmd)}\n{proc.stderr[-2000:]}")
    return proc.stdout


def verify_signature(reference: str) -> None:
    """Notation verification against the pinned trust policy.

    This runs before any twin is touched. An unsigned or untrusted-chain bundle
    must never become a device's desired state, because a device that later comes
    online would faithfully try to converge to it.
    """
    proc = subprocess.run(["notation", "verify", reference], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"REFUSING ROLLOUT: signature verification failed for {reference}\n"
            f"{proc.stderr[-2000:]}"
        )
    log.info("signature verified: %s", reference)


def acquire_lock(hub: str, release_id: str) -> None:
    """Fleet-wide advisory lock against concurrent rollouts.

    Two rollouts targeting overlapping rings interleave twin patches and leave
    devices in a mixture of states that no single rollback restores. Do not
    bypass this.
    """
    existing = json.loads(
        az(
            "iot",
            "hub",
            "device-twin",
            "list",
            "-n",
            hub,
            "--query",
            f"[?tags.{LOCK_TWIN_TAG}!=null].tags.{LOCK_TWIN_TAG}",
            "-o",
            "json",
        )
        or "[]"
    )
    active = [e for e in existing if e and e != release_id]
    if active:
        raise SystemExit(f"REFUSING ROLLOUT: another rollout holds the fleet lock: {active[0]}")


def patch_ring(hub: str, ring: Ring, bundle: str, release_id: str, *, dry_run: bool = False) -> int:
    """Patch desired properties for every device matching the ring condition."""
    devices = json.loads(
        az(
            "iot",
            "hub",
            "device-twin",
            "list",
            "-n",
            hub,
            "--query",
            f"[?{_to_jmespath(ring.target_condition)}].deviceId",
            "-o",
            "json",
        )
        or "[]"
    )
    if not devices:
        log.warning("ring %s matched no devices", ring.name)
        return 0

    desired = {
        "properties": {
            "desired": {
                "perception": {
                    "bundle": bundle,
                    "shadow_mode": ring.shadow_mode,
                    "release_id": release_id,
                }
            }
        },
        "tags": {LOCK_TWIN_TAG: release_id, "ring": ring.name},
    }

    log.info(
        "patching %d device(s) in ring %s -> %s%s",
        len(devices),
        ring.name,
        bundle,
        "  [DRY RUN]" if dry_run else "",
    )
    if dry_run:
        print(json.dumps({"devices": devices, "patch": desired}, indent=2))
        return len(devices)

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
            json.dumps(desired),
        )
    return len(devices)


def _to_jmespath(condition: str) -> str:
    """Translate the IoT Hub query fragment in rings.yaml to JMESPath.

    IoT Hub query syntax and the CLI's client-side --query are not the same
    language; keeping the translation in one small function is preferable to
    maintaining two spellings of every ring condition.
    """
    return condition.replace(" = ", " == ").replace("'", "'")


def watch_health(hub: str, ring: Ring, release_id: str, *, minutes: int) -> bool:
    """Poll the health contract through the settle window.

    Returns False if any rollback predicate fires, in which case the caller rolls
    back rather than advancing. The predicates themselves also run server-side as
    Azure Monitor rules -- this loop is the fast path during a supervised
    rollout, not the only line of defence.
    """
    from rollback import evaluate_predicates, fetch_health  # local module

    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        health = fetch_health(hub, ring.name, release_id)
        fired = evaluate_predicates(health)
        if fired:
            log.error("rollback predicate(s) fired during soak: %s", fired)
            return False
        remaining = int(deadline - time.monotonic())
        log.info("ring %s healthy; %ds of settle window remaining", ring.name, remaining)
        time.sleep(min(60, max(5, remaining)))
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="edgeforge ring rollout")
    p.add_argument("--hub", required=True, help="IoT Hub name")
    p.add_argument("--bundle", required=True, help="e.g. hazard-seg:41")
    p.add_argument("--reference", required=True, help="full ACR reference with digest")
    p.add_argument("--ring", required=True)
    p.add_argument("--release-id", required=True)
    p.add_argument("--settle-minutes", type=int, default=10)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    rings = load_rings()
    if a.ring not in rings:
        raise SystemExit(f"unknown ring {a.ring!r}; known: {sorted(rings)}")
    ring = rings[a.ring]

    verify_signature(a.reference)
    if not a.dry_run:
        acquire_lock(a.hub, a.release_id)

    count = patch_ring(a.hub, ring, a.bundle, a.release_id, dry_run=a.dry_run)
    if a.dry_run or count == 0:
        return 0

    if not watch_health(a.hub, ring, a.release_id, minutes=a.settle_minutes):
        log.error("settle window failed; invoking rollback for ring %s", ring.name)
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("rollback.py")),
                "--hub",
                a.hub,
                "--ring",
                ring.name,
            ],
            check=True,
        )
        return 1

    log.info(
        "ring %s at %s is healthy. %s",
        ring.name,
        a.bundle,
        "Advancing automatically."
        if ring.auto_advance
        else "Manual advance required for the next ring.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
