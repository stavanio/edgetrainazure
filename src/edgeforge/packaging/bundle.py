"""Edge bundle assembly, model card, and signing.

The deliverable is one OCI artifact in ACR containing everything needed to
reproduce the robot's inference behaviour exactly. Shipping the preprocessing
config *with* the engine is not tidiness -- train/serve preprocessing skew is
the most common cause of "great in eval, mediocre in the field", and making the
config an artifact rather than code on both sides removes the failure mode.

Signing is the single most important control in the system. The robot verifies
the Notation signature against a trust store pinned at manufacture and refuses
to load anything that does not chain to it. That is what stops a compromised
pipeline, registry, or network position from putting arbitrary inference
behaviour onto a 30-tonne machine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from edgeforge.evaluation.gates import EvaluationResult
from edgeforge.optimize.export_and_build import BuildResult

log = logging.getLogger(__name__)

BUNDLE_MEDIA_TYPE = "application/vnd.edgeforge.bundle.v1+json"


@dataclass(slots=True)
class BundleSpec:
    model: str
    version: int
    target_tag: str  # e.g. "orin-agx-64-jp6.0"
    registry: str  # ACR login server
    repository: str = "edge-bundles"

    @property
    def reference(self) -> str:
        return f"{self.registry}/{self.repository}/{self.model}:{self.version}-{self.target_tag}"


@dataclass(slots=True)
class BundleManifest:
    model: str
    version: int
    target_tag: str
    created_utc: str
    source_commit: str
    snapshot_merkle_root: str
    aml_job_id: str
    files: dict[str, str] = field(default_factory=dict)  # relative path -> sha256

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- model card --------------------------------------------------------------


def render_model_card(
    spec: BundleSpec,
    evaluation: EvaluationResult,
    build: BuildResult,
    *,
    source_commit: str,
    known_limits: list[str] | None = None,
) -> str:
    """Generated from gate results, not hand-written.

    A hand-written model card drifts from reality within two releases. Generating
    it means the card is always what the gates actually measured, which is the
    property that makes it usable as safety-case evidence.
    """
    cl = evaluation.closed_loop
    worst_slices = sorted(evaluation.per_slice.items(), key=lambda kv: kv[1])[:8]

    limits = known_limits or [
        "Not validated above 45 C ambient; sustained-power measurements assume the "
        "production thermal solution.",
        "Whiteout-class particulate frames are rejected at the quality gate and are "
        "therefore out of distribution for this model. The OOD signal, not the "
        "detection output, is the correct consumer in those conditions.",
        "Personnel detection is validated for standing, walking, and crouching poses. "
        "Prone-pose coverage is present but thin; see the slice table.",
    ]

    rows = "\n".join(f"| `{k}` | {v:.2f} |" for k, v in worst_slices)
    gate_rows = "\n".join(
        f"| {o.tier} | `{o.gate}` | {o.verdict.value} | {o.detail} |" for o in evaluation.outcomes
    )

    return f"""# Model card — `{spec.model}` v{spec.version} ({spec.target_tag})

Generated {datetime.now(UTC).isoformat(timespec="seconds")} by `edgeforge.packaging.bundle`.
Do not edit by hand; regenerate from the evaluation run.

## Identity and lineage

| | |
|---|---|
| Model | `{spec.model}` |
| Version | {spec.version} |
| Target | `{spec.target_tag}` (SM {build.target.sm}, TensorRT {build.target.tensorrt}) |
| Training snapshot | `{evaluation.snapshot_merkle_root}` |
| Golden set | `{evaluation.golden_set_version}` |
| Source commit | `{source_commit}` |
| Bundle reference | `{spec.reference}` |

## Intended use

On-vehicle perception for a Class-4 subterranean haulage and inspection robot:
hazard segmentation, drivable-surface estimation, personnel detection, and
equipment-anomaly detection from the forward stereo pair.

**Not intended** as the sole basis for any safety function. Outputs feed a safety
envelope that combines them with geometric and kinematic constraints.

## Aggregate performance (human-labeled golden set)

| Metric | Value |
|---|---|
| mAP@50:95 | {evaluation.aggregate.map_50_95:.2f} |
| mIoU | {evaluation.aggregate.miou:.2f} |
| Expected calibration error | {evaluation.aggregate.expected_calibration_error:.4f} |

## Closed-loop behaviour ({cl.scenarios_run} scenarios)

| Metric | Value |
|---|---|
| Time to detect | {cl.time_to_detect_s:.3f} s |
| Time-to-brake margin | {cl.time_to_brake_margin_s:.3f} s |
| False stops per hour | {cl.false_stops_per_hour:.3f} |
| **Missed personnel events** | **{cl.missed_personnel_events}** |
| ID switches per 1k frames | {cl.id_switches_per_1k:.2f} |

## Weakest slices

| Taxonomy cell | mAP |
|---|---|
{rows}

## On-silicon measurement

| Metric | Value | Budget |
|---|---|---|
| Latency p50 | {build.latency_p50_ms:.1f} ms | — |
| Latency p99 | {build.latency_p99_ms:.1f} ms | {build.target.latency_budget_p99_ms:.0f} ms |
| Sustained power | {build.power_avg_w:.1f} W | {build.target.power_budget_w:.0f} W |
| Peak power | {build.power_peak_w:.1f} W | — |
| SoC steady temp | {build.soc_temp_steady_c:.1f} °C | — |
| Thermally throttled | {build.throttled_pct:.2f} % | < 1 % |
| INT8 accuracy delta | {build.accuracy_delta_map:.2f} mAP | < 0.8 |
| ONNX parity (max abs) | {build.onnx_parity_max_abs_diff:.2e} | < 1e-4 |

Measured on the HIL rack under the representative concurrent-load profile, not
in isolation. See `docs/04-edge-plane.md` §4.1.

## Gate results

| Tier | Gate | Verdict | Detail |
|---|---|---|---|
{gate_rows}

## Known limitations

{chr(10).join(f"- {lim}" for lim in limits)}

## Rollback

Previous known-good bundle remains resident on every device. Rollback is a device
twin patch and does not require network access to a registry.
"""


# --- assembly ----------------------------------------------------------------


def assemble(
    workdir: Path,
    spec: BundleSpec,
    build: BuildResult,
    evaluation: EvaluationResult,
    *,
    preprocess_json: str,
    postprocess_json: str,
    runtime_json: str,
    source_commit: str,
    aml_job_id: str,
) -> Path:
    """Lay out the bundle directory and write the manifest.

    Refuses to assemble a bundle whose build or gates failed. This check exists
    because the alternative -- assembling and relying on a later stage to notice
    -- has failed in practice more than once.
    """
    if not build.passed:
        raise RuntimeError(
            "refusing to assemble: on-silicon build failed:\n  " + "\n  ".join(build.failures)
        )
    if not evaluation.passed:
        failures = [o.gate for o in evaluation.outcomes if o.blocking]
        raise RuntimeError(f"refusing to assemble: gates failed: {failures}")

    root = workdir / f"{spec.model}-{spec.version}-{spec.target_tag}"
    for sub in ("engine", "config", "calib"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    shutil.copy2(build.engine_path, root / "engine" / f"{spec.model}.plan")
    # The ONNX ships too: a JetPack bump requires a rebuild, and having the source
    # in the bundle means that rebuild does not need the training environment.
    shutil.copy2(build.onnx_path, root / "engine" / f"{spec.model}.onnx")
    shutil.copy2(build.calibration_cache, root / "calib" / "calibration.cache")

    (root / "config" / "preprocess.json").write_text(preprocess_json)
    (root / "config" / "postprocess.json").write_text(postprocess_json)
    (root / "config" / "runtime.json").write_text(runtime_json)
    (root / "model_card.md").write_text(
        render_model_card(spec, evaluation, build, source_commit=source_commit)
    )
    (root / "evaluation.json").write_text(evaluation.to_json())
    (root / "build.json").write_text(build.to_json())

    manifest = BundleManifest(
        model=spec.model,
        version=spec.version,
        target_tag=spec.target_tag,
        created_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        source_commit=source_commit,
        snapshot_merkle_root=evaluation.snapshot_merkle_root,
        aml_job_id=aml_job_id,
        files={
            str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()
        },
    )
    (root / "manifest.json").write_text(manifest.to_json())
    log.info("assembled bundle at %s (%d files)", root, len(manifest.files))
    return root


# --- publish and sign --------------------------------------------------------


def push(root: Path, spec: BundleSpec) -> str:
    """Push as an OCI artifact via ORAS. Returns the resolved digest."""
    cmd = [
        "oras",
        "push",
        spec.reference,
        "--artifact-type",
        BUNDLE_MEDIA_TYPE,
        "--format",
        "json",
        ".",
    ]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"oras push failed:\n{proc.stderr[-4000:]}")
    digest = json.loads(proc.stdout)["digest"]
    log.info("pushed %s -> %s", spec.reference, digest)
    return digest


def sign(reference_with_digest: str, key_id: str) -> None:
    """Sign with Notation using the HSM-backed Key Vault key.

    The runner can *use* the key and cannot *read* it — Key Vault Crypto User,
    not Key Vault Crypto Officer (infra/ml.tf). A compromised runner can sign one
    bad bundle, which the two-person promotion gate still has to approve; it
    cannot walk away with the fleet's signing identity.
    """
    cmd = [
        "notation",
        "sign",
        reference_with_digest,
        "--plugin",
        "azure-kv",
        "--id",
        key_id,
        "--signature-format",
        "cose",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"notation sign failed:\n{proc.stderr[-4000:]}")
    log.info("signed %s", reference_with_digest)


def generate_sbom(root: Path, spec: BundleSpec) -> Path:
    """SPDX SBOM, attached as an OCI referrer.

    Defender for Containers scans it, so a new critical CVE against something
    inside a *shipped* bundle raises a fleet work item rather than being
    discovered at the next release.
    """
    out = root / "sbom.spdx.json"
    proc = subprocess.run(
        ["syft", "dir:.", "-o", "spdx-json"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"syft failed:\n{proc.stderr[-2000:]}")
    out.write_text(proc.stdout)
    log.info("wrote SBOM for %s", spec.reference)
    return out


__all__ = [
    "BUNDLE_MEDIA_TYPE",
    "BundleManifest",
    "BundleSpec",
    "assemble",
    "generate_sbom",
    "push",
    "render_model_card",
    "sha256_file",
    "sign",
]
