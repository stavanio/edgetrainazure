"""ONNX export, INT8 calibration, TensorRT build, and on-silicon measurement.

This runs on the HIL rack -- real Orin AGX modules on the production carrier and
thermal solution, Arc-enabled and registered as self-hosted runners -- and
nowhere else. TensorRT engines are tied to compute capability, TensorRT version,
and often the exact driver of the machine that built them; an engine
cross-compiled from a cloud A100 either refuses to load on an Orin or silently
behaves differently.

Building on real hardware also buys the measurements a cloud GPU cannot give:
p99 under the concurrent load a robot actually generates, sustained power once
the SoC has settled, and thermal behaviour at mine ambient rather than lab
ambient.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

ONNX_OPSET = 17  # pinned; a bump is a deliberate, tested change


@dataclass(frozen=True, slots=True)
class Target:
    """One edge SKU. Engines are built per target, never shared."""

    name: str  # e.g. "orin-agx-64"
    sm: str  # e.g. "8.7"
    jetpack: str  # e.g. "6.0"
    tensorrt: str  # e.g. "10.0.1"
    power_budget_w: float = 40.0
    latency_budget_p99_ms: float = 45.0
    dla_cores: int = 2

    @property
    def tag(self) -> str:
        return f"{self.name}-jp{self.jetpack}"


@dataclass(slots=True)
class BuildResult:
    target: Target
    engine_path: str
    onnx_path: str
    calibration_cache: str

    onnx_parity_max_abs_diff: float
    latency_p50_ms: float
    latency_p99_ms: float
    power_avg_w: float
    power_peak_w: float
    soc_temp_steady_c: float
    throttled_pct: float
    engine_load_ms: float
    accuracy_delta_map: float  # INT8 engine vs FP32 checkpoint, golden set

    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def failures(self) -> list[str]:
        f: list[str] = []
        if self.latency_p99_ms > self.target.latency_budget_p99_ms:
            f.append(
                f"p99 {self.latency_p99_ms:.1f}ms exceeds "
                f"{self.target.latency_budget_p99_ms:.1f}ms budget"
            )
        if self.power_avg_w > self.target.power_budget_w:
            f.append(
                f"sustained power {self.power_avg_w:.1f}W exceeds "
                f"{self.target.power_budget_w:.1f}W budget"
            )
        if self.accuracy_delta_map > 0.8:
            f.append(f"INT8 accuracy loss {self.accuracy_delta_map:.2f} mAP exceeds 0.8")
        if self.onnx_parity_max_abs_diff > 1e-4:
            f.append(
                f"ONNX parity {self.onnx_parity_max_abs_diff:.2e} exceeds 1e-4 -- "
                "export is not faithful to the checkpoint"
            )
        if self.throttled_pct > 1.0:
            f.append(
                f"thermally throttled {self.throttled_pct:.1f}% of the measurement "
                "window; latency figures are not trustworthy"
            )
        return f

    def to_json(self) -> str:
        d = asdict(self)
        d["passed"] = self.passed
        d["failures"] = self.failures
        return json.dumps(d, indent=2, sort_keys=True, default=str)


# --- 1. ONNX export ----------------------------------------------------------


def export_onnx(checkpoint: Path, out: Path, *, input_hw: tuple[int, int]) -> Path:
    """Export with a fixed batch and fixed spatial dims.

    Dynamic axes are deliberately avoided except where genuinely needed: TensorRT
    optimises a fixed shape considerably better, and the robot only ever runs one
    resolution -- the one in the shipped preprocess spec.
    """
    import torch

    from edgeforge.training.train_perception import PerceptionNet

    model = PerceptionNet(width=32).eval()
    model.load_state_dict(torch.load(checkpoint, map_location="cpu"))

    h, w = input_hw
    dummy = torch.randn(1, 3, h, w)
    out.parent.mkdir(parents=True, exist_ok=True)

    torch.onnx.export(
        model,
        dummy,
        str(out),
        input_names=["image"],
        output_names=[
            "hazard_seg",
            "drivable_surface",
            "personnel_cls",
            "personnel_reg",
            "personnel_obj",
        ],
        opset_version=ONNX_OPSET,
        do_constant_folding=True,
        dynamo=False,
    )
    log.info("exported ONNX to %s (opset %d)", out, ONNX_OPSET)
    return out


def check_parity(checkpoint: Path, onnx_path: Path, samples: Sequence[np.ndarray]) -> float:
    """Max absolute difference between PyTorch and ONNX Runtime over real frames.

    Random noise will not catch a broken export -- a mis-wired normalisation or a
    dropped branch can look fine on noise and be badly wrong on structure. Use
    frames from the snapshot.
    """
    import onnxruntime as ort
    import torch

    from edgeforge.training.train_perception import PerceptionNet

    model = PerceptionNet(width=32).eval()
    model.load_state_dict(torch.load(checkpoint, map_location="cpu"))
    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])

    worst = 0.0
    for arr in samples:
        x = arr[None].astype(np.float32)
        with torch.no_grad():
            torch_out = model(torch.from_numpy(x))["hazard_seg"].numpy()
        onnx_out = sess.run(["hazard_seg"], {"image": x})[0]
        worst = max(worst, float(np.abs(torch_out - onnx_out).max()))

    log.info("ONNX parity over %d frames: max |diff| = %.3e", len(samples), worst)
    return worst


# --- 2. INT8 calibration -----------------------------------------------------


def build_calibration_set(snapshot_records, *, size: int = 1_024, seed: int = 7) -> list[str]:
    """Stratified calibration set, one slot per taxonomy cell in proportion to
    that cell's sampling floor.

    Calibrating on a random sample is a classic and expensive mistake: the random
    sample under-represents rare cells, so quantization error concentrates
    precisely in the conditions where accuracy matters most. The cost of getting
    this right is nothing; the cost of getting it wrong shows up only in the
    field.
    """
    from collections import defaultdict

    from edgeforge.taxonomy import sampling_floor

    rng = np.random.default_rng(seed)
    by_cell: dict[str, list] = defaultdict(list)
    for r in snapshot_records:
        by_cell[r.cell.key].append(r)

    weights = {k: sampling_floor(v[0].cell) for k, v in by_cell.items()}
    total_weight = sum(weights.values()) or 1

    chosen: list[str] = []
    for key, records in by_cell.items():
        quota = max(1, round(size * weights[key] / total_weight))
        idx = rng.choice(len(records), min(quota, len(records)), replace=False)
        chosen += [records[i].image_path for i in idx]

    rng.shuffle(chosen)
    log.info("calibration set: %d frames across %d cells", len(chosen), len(by_cell))
    return chosen[:size]


# --- 3. TensorRT build -------------------------------------------------------

# Layers kept in FP16 regardless of INT8 mode. The first convolution sees raw
# pixel statistics and the final heads produce the logits that feed the safety
# envelope; quantizing either costs far more accuracy than the latency it buys.
FP16_KEEP_PATTERNS = ("stem", "personnel_det", "classifier")


def build_engine(
    onnx_path: Path,
    out: Path,
    target: Target,
    calibration_cache: Path,
    *,
    int8: bool = True,
    dla_heads: bool = True,
) -> Path:
    """Invoke trtexec on the target device.

    Shelling out to trtexec rather than using the Python API is deliberate: the
    resulting command is reproducible by hand on the device when something goes
    wrong at 2am, which the Python builder is not.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "trtexec",
        f"--onnx={onnx_path}",
        f"--saveEngine={out}",
        "--fp16",
        "--builderOptimizationLevel=4",
        "--useSpinWait",
        "--noDataTransfers",
    ]
    if int8:
        cmd += ["--int8", f"--calib={calibration_cache}"]
        cmd += [f"--layerPrecisions={p}:fp16" for p in FP16_KEEP_PATTERNS]
    if dla_heads and target.dla_cores:
        # Offloading the anomaly head to DLA frees GPU for SLAM, which is the
        # actual contended resource on the robot.
        cmd += ["--useDLACore=0", "--allowGPUFallback"]

    log.info("building engine: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"trtexec failed ({proc.returncode}):\n{proc.stderr[-4000:]}")
    log.info("engine written to %s", out)
    return out


# --- 4. On-silicon measurement -----------------------------------------------


@dataclass(slots=True)
class LoadProfile:
    """The concurrent load a real robot generates on the same SoC.

    Measuring an engine in isolation gives a number that will not survive contact
    with the vehicle. If field p99 later diverges from HIL p99, fix this profile
    first -- every measurement is wrong until it matches (docs/08-runbook.md P3).
    """

    slam_gpu_utilisation: float = 0.35
    logging_io_mbps: float = 180.0
    planner_cpu_cores: int = 3
    duration_s: float = 1_200.0  # 20 min: long enough for thermal steady state
    ambient_c: float = 45.0


def measure(engine: Path, target: Target, profile: LoadProfile | None = None) -> dict[str, float]:
    """Run the engine under representative load and sample telemetry.

    The 20-minute duration is not padding. An Orin that fits the latency budget
    at t=0 frequently does not at t=20min once clocks drop, and a model that only
    passes cold is a model that fails on shift.
    """
    profile = profile or LoadProfile()
    log.info(
        "measuring %s on %s under load for %.0fs at %.0f C ambient",
        engine.name,
        target.name,
        profile.duration_s,
        profile.ambient_c,
    )

    latencies: list[float] = []
    powers: list[float] = []
    temps: list[float] = []
    throttle_samples = 0
    total_samples = 0

    start = time.monotonic()
    load = _start_background_load(profile)
    try:
        t0 = time.perf_counter()
        _load_engine(engine)
        engine_load_ms = (time.perf_counter() - t0) * 1e3

        while time.monotonic() - start < profile.duration_s:
            t0 = time.perf_counter()
            _infer_once(engine)
            latencies.append((time.perf_counter() - t0) * 1e3)

            telem = _read_soc_telemetry()
            powers.append(telem["power_w"])
            temps.append(telem["temp_c"])
            throttle_samples += int(telem["throttled"])
            total_samples += 1
    finally:
        _stop_background_load(load)

    arr = np.asarray(latencies)
    return {
        "latency_p50_ms": float(np.percentile(arr, 50)),
        "latency_p99_ms": float(np.percentile(arr, 99)),
        "power_avg_w": float(np.mean(powers)),
        "power_peak_w": float(np.max(powers)),
        # Steady state is the last quarter of the window, not the mean: the mean
        # is dragged down by the cold start we do not care about.
        "soc_temp_steady_c": float(np.mean(temps[-len(temps) // 4 :])),
        "throttled_pct": 100.0 * throttle_samples / max(1, total_samples),
        "engine_load_ms": engine_load_ms,
    }


# Device-side hooks. Implemented by the HIL runner image against tegrastats and
# the TensorRT runtime; stubbed here so the module imports on any machine.
def _start_background_load(profile: LoadProfile):  # pragma: no cover
    raise NotImplementedError("implemented by the HIL runner image")


def _stop_background_load(handle) -> None:  # pragma: no cover
    raise NotImplementedError("implemented by the HIL runner image")


def _load_engine(engine: Path):  # pragma: no cover
    raise NotImplementedError("implemented by the HIL runner image")


def _infer_once(engine) -> None:  # pragma: no cover
    raise NotImplementedError("implemented by the HIL runner image")


def _read_soc_telemetry() -> dict[str, float]:  # pragma: no cover
    raise NotImplementedError("implemented by the HIL runner image (tegrastats)")


__all__ = [
    "FP16_KEEP_PATTERNS",
    "ONNX_OPSET",
    "BuildResult",
    "LoadProfile",
    "Target",
    "build_calibration_set",
    "build_engine",
    "check_parity",
    "export_onnx",
    "measure",
]
