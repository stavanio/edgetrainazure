"""Quality gates: /raw -> /clean.

Rejection is never silent. Every rejected frame is written to the reject ledger
with its reason and metric value, because the rejection-rate time series is
itself a fleet health signal -- a camera going soft shows up as a rising blur
rejection rate weeks before anyone notices it visually.

Run as an AML component (see pipelines/aml/components/quality-gates.yml) or
locally against a Delta path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

log = logging.getLogger(__name__)


class Reject(StrEnum):
    BLUR = "blur"
    EXPOSURE = "exposure"
    OCCLUSION = "occlusion"
    SYNC = "sync"
    INTEGRITY = "integrity"
    CALIBRATION = "calibration"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Tuned on this workload. Retune per camera generation, not per site.

    Site-specific tuning is a trap: it produces a dataset whose quality bar
    varies by site, and the model then underperforms at whichever site had the
    loosest gate.
    """

    blur_laplacian_var_min: float = 55.0
    exposure_clip_frac_max: float = 0.12
    haze_dark_channel_max: float = 0.62
    sync_skew_ms_max: float = 8.0
    calibration_age_days_max: float = 30.0


@dataclass(slots=True)
class FrameMetrics:
    frame_id: str
    blur_laplacian_var: float
    exposure_clip_frac: float
    haze: float
    sync_skew_ms: float
    calibration_age_days: float
    decoder_ok: bool


@dataclass(slots=True)
class GateResult:
    frame_id: str
    passed: bool
    rejections: tuple[Reject, ...]
    metrics: dict[str, float]


# --- metric implementations --------------------------------------------------


def laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the Laplacian. Low variance == few edges == blurred.

    Deliberately computed on the luma plane at native resolution; downsampling
    first destroys exactly the high-frequency content being measured.
    """
    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    # 'valid' convolution via stride tricks keeps this dependency-light.
    h, w = gray.shape
    if h < 3 or w < 3:
        return 0.0
    windows = np.lib.stride_tricks.sliding_window_view(gray.astype(np.float32), (3, 3))
    lap = np.einsum("ijkl,kl->ij", windows, k)
    return float(lap.var())


def exposure_clip_fraction(gray: np.ndarray, low: int = 2, high: int = 253) -> float:
    """Fraction of pixels pinned at either end of the range.

    Both ends matter here: underground frames clip black in unlit volumes and
    clip white against headlamps and retroreflective PPE in the same frame.
    """
    total = gray.size
    if total == 0:
        return 1.0
    clipped = np.count_nonzero(gray <= low) + np.count_nonzero(gray >= high)
    return float(clipped / total)


def dark_channel_haze(rgb: np.ndarray, patch: int = 15) -> float:
    """Dark-channel-prior haze estimate, normalised to [0, 1].

    This is the domain-specific gate and it is the one that matters most. A
    naive pipeline trains happily on airborne-dust frames a human could not
    interpret, and the model learns to emit confident predictions in a whiteout.

    Haze-free outdoor-style scenes have near-zero minimum channel intensity in
    most local patches; a dust cloud raises the floor everywhere.
    """
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        raise ValueError("dark_channel_haze expects an HxWx3 array")
    min_channel = rgb[:, :, :3].min(axis=2).astype(np.float32)
    h, w = min_channel.shape
    if h < patch or w < patch:
        return float(min_channel.mean() / 255.0)
    windows = np.lib.stride_tricks.sliding_window_view(min_channel, (patch, patch))
    dark = windows.min(axis=(2, 3))
    return float(dark.mean() / 255.0)


def sync_skew_ms(timestamps_ns: Sequence[int]) -> float:
    """Max inter-sensor timestamp spread for one capture group.

    A stereo pair that drifts apart silently destroys depth. This catches PTP
    problems that no amount of image-space checking would find.
    """
    if len(timestamps_ns) < 2:
        return 0.0
    return (max(timestamps_ns) - min(timestamps_ns)) / 1e6


# --- gate --------------------------------------------------------------------


def evaluate(m: FrameMetrics, t: Thresholds | None = None) -> GateResult:
    t = t or Thresholds()
    rejections: list[Reject] = []

    if not m.decoder_ok:
        rejections.append(Reject.INTEGRITY)
    if m.blur_laplacian_var < t.blur_laplacian_var_min:
        rejections.append(Reject.BLUR)
    if m.exposure_clip_frac > t.exposure_clip_frac_max:
        rejections.append(Reject.EXPOSURE)
    if m.haze > t.haze_dark_channel_max:
        rejections.append(Reject.OCCLUSION)
    if m.sync_skew_ms > t.sync_skew_ms_max:
        rejections.append(Reject.SYNC)

    # Calibration staleness flags but does not reject: the frame is still usable,
    # the extrinsics just are not trustworthy for depth-derived labels.
    flags: list[Reject] = []
    if m.calibration_age_days > t.calibration_age_days_max:
        flags.append(Reject.CALIBRATION)

    return GateResult(
        frame_id=m.frame_id,
        passed=not rejections,
        rejections=tuple(rejections),
        metrics={
            "blur_laplacian_var": m.blur_laplacian_var,
            "exposure_clip_frac": m.exposure_clip_frac,
            "haze": m.haze,
            "sync_skew_ms": m.sync_skew_ms,
            "calibration_age_days": m.calibration_age_days,
            "calibration_stale": float(bool(flags)),
        },
    )


def run(frames: Iterable[FrameMetrics], thresholds: Thresholds | None = None):
    """Apply gates to a batch, returning (passed, reject_ledger).

    The ledger is written to /clean/_rejects as Delta. Watch its per-camera
    rate over time -- see docs/08-runbook.md §8.4.
    """
    passed: list[GateResult] = []
    ledger: list[dict] = []

    for m in frames:
        result = evaluate(m, thresholds)
        if result.passed:
            passed.append(result)
        else:
            ledger.append(
                {
                    "frame_id": result.frame_id,
                    "reasons": [r.value for r in result.rejections],
                    **result.metrics,
                }
            )

    total = len(passed) + len(ledger)
    if total:
        log.info(
            "quality gates: %d/%d passed (%.1f%% rejected)",
            len(passed),
            total,
            100.0 * len(ledger) / total,
        )
        if len(ledger) / total > 0.35:
            # Not an error -- a whole shift in heavy dust legitimately looks like
            # this -- but it is worth surfacing loudly, because the other cause is
            # a hardware fault and the two are indistinguishable from the metrics.
            log.warning(
                "rejection rate above 35%%; check per-camera breakdown before "
                "assuming this is environmental"
            )

    return passed, ledger


__all__ = [
    "FrameMetrics",
    "GateResult",
    "Reject",
    "Thresholds",
    "dark_channel_haze",
    "evaluate",
    "exposure_clip_fraction",
    "laplacian_variance",
    "run",
    "sync_skew_ms",
]
