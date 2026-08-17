"""Tests for the curation stage.

These focus on the properties that, if they broke, would waste a full 11-day
training cycle or silently produce a dataset that fails a slice gate.
"""

from __future__ import annotations

import numpy as np
import pytest

from edgeforge.curation.dedupe_and_sample import Frame, dedupe, hamming, stratified_sample
from edgeforge.curation.quality_gates import (
    FrameMetrics,
    Reject,
    evaluate,
    exposure_clip_fraction,
    laplacian_variance,
    run,
)
from edgeforge.curation.snapshot import FileEntry, build, manifest_json, merkle_root, verify
from edgeforge.taxonomy import (
    Actors,
    Cell,
    Geometry,
    Illumination,
    Particulate,
    Surface,
    sampling_floor,
    slice_tolerance,
)


def cell(
    illum=Illumination.BRIGHT,
    part=Particulate.CLEAR,
    surf=Surface.DRY_COMPACT,
    geom=Geometry.DRIFT,
    act=Actors.NONE,
) -> Cell:
    return Cell(illum, part, surf, geom, act)


SAFETY_CELL = cell(Illumination.DARK, Particulate.HEAVY, act=Actors.PERSONNEL)
COMMON_CELL = cell()


# --- taxonomy ----------------------------------------------------------------


def test_safety_critical_requires_personnel():
    assert not cell(Illumination.DARK, Particulate.WHITEOUT).safety_critical
    assert SAFETY_CELL.safety_critical


def test_safety_critical_cells_get_no_slice_headroom():
    # This is the property that stops "improved overall by getting better at easy
    # frames" from being promotable.
    assert slice_tolerance(SAFETY_CELL) == 0.0
    assert slice_tolerance(COMMON_CELL) > 0.0


def test_safety_critical_cells_have_the_highest_sampling_floor():
    assert sampling_floor(SAFETY_CELL) > sampling_floor(COMMON_CELL)
    assert sampling_floor(cell(part=Particulate.WHITEOUT)) == 0


def test_cell_key_roundtrips():
    assert Cell.from_key(SAFETY_CELL.key) == SAFETY_CELL


# --- quality gates -----------------------------------------------------------


def metrics(**kw) -> FrameMetrics:
    base = dict(
        frame_id="f",
        blur_laplacian_var=200.0,
        exposure_clip_frac=0.01,
        haze=0.1,
        sync_skew_ms=1.0,
        calibration_age_days=3.0,
        decoder_ok=True,
    )
    base.update(kw)
    return FrameMetrics(**base)


def test_clean_frame_passes():
    assert evaluate(metrics()).passed


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"blur_laplacian_var": 10.0}, Reject.BLUR),
        ({"exposure_clip_frac": 0.5}, Reject.EXPOSURE),
        ({"haze": 0.9}, Reject.OCCLUSION),
        ({"sync_skew_ms": 40.0}, Reject.SYNC),
        ({"decoder_ok": False}, Reject.INTEGRITY),
    ],
)
def test_each_gate_rejects(kwargs, expected):
    result = evaluate(metrics(**kwargs))
    assert not result.passed
    assert expected in result.rejections


def test_stale_calibration_flags_but_does_not_reject():
    # The frame is still usable; only depth-derived labels are untrustworthy.
    result = evaluate(metrics(calibration_age_days=90.0))
    assert result.passed
    assert result.metrics["calibration_stale"] == 1.0


def test_rejections_land_in_the_ledger_with_reasons():
    passed, ledger = run(
        [metrics(frame_id="ok"), metrics(frame_id="blurry", blur_laplacian_var=1.0)]
    )
    assert [p.frame_id for p in passed] == ["ok"]
    assert ledger[0]["frame_id"] == "blurry"
    assert Reject.BLUR.value in ledger[0]["reasons"]


def test_laplacian_variance_separates_sharp_from_flat():
    flat = np.full((64, 64), 128, dtype=np.uint8)
    sharp = np.tile(np.array([0, 255], dtype=np.uint8), (64, 32))
    assert laplacian_variance(flat) < laplacian_variance(sharp)


def test_exposure_clip_fraction_counts_both_ends():
    arr = np.array([[0, 255, 128, 128]], dtype=np.uint8)
    assert exposure_clip_fraction(arr) == pytest.approx(0.5)


# --- dedupe ------------------------------------------------------------------


def frame(
    fid: str,
    *,
    phash: int,
    priority: float,
    ts: int,
    c: Cell = COMMON_CELL,
    robot: str = "mr1-001",
    emb: np.ndarray | None = None,
) -> Frame:
    if emb is None:
        emb = np.zeros(8, dtype=np.float32)
        emb[hash(fid) % 8] = 1.0
    return Frame(
        frame_id=fid,
        cell=c,
        phash=phash,
        embedding=emb,
        site="alpha",
        robot=robot,
        timestamp_ns=ts,
        priority=priority,
        reason="novel",
    )


def test_hamming():
    assert hamming(0b1010, 0b1000) == 1


def test_dedupe_keeps_the_highest_priority_frame_in_a_group():
    # The robot already told us which frame it found most interesting. Keeping an
    # arbitrary representative throws that judgement away.
    frames = [
        frame("low", phash=0b1010, priority=0.1, ts=0),
        frame("high", phash=0b1010, priority=0.9, ts=1_000_000_000),
        frame("mid", phash=0b1010, priority=0.5, ts=2_000_000_000),
    ]
    kept = dedupe(frames, embedding_min_distance=0.0)
    assert [f.frame_id for f in kept] == ["high"]


def test_dedupe_keeps_distinct_scenes():
    frames = [
        frame("a", phash=0b0000, priority=0.5, ts=0),
        frame("b", phash=0b1111_1111, priority=0.5, ts=60_000_000_000),
    ]
    assert len(dedupe(frames, embedding_min_distance=0.0)) == 2


def test_dedupe_does_not_group_across_robots():
    frames = [
        frame("a", phash=0b1010, priority=0.5, ts=0, robot="mr1-001"),
        frame("b", phash=0b1010, priority=0.5, ts=0, robot="mr1-002"),
    ]
    assert len(dedupe(frames, embedding_min_distance=0.0)) == 2


def test_dedupe_thins_semantic_duplicates_by_embedding():
    shared = np.ones(8, dtype=np.float32) / np.sqrt(8)
    frames = [
        frame("a", phash=0b0000, priority=0.9, ts=0, emb=shared),
        frame("b", phash=0b1111, priority=0.4, ts=60_000_000_000, emb=shared.copy()),
    ]
    kept = dedupe(frames, embedding_min_distance=0.2)
    assert [f.frame_id for f in kept] == ["a"]


# --- stratified sampling -----------------------------------------------------


def test_rare_safety_cell_survives_a_flood_of_common_frames():
    """The core property. Uniform sampling would drown the rare cell; the floor
    is what stops the model being excellent at empty drifts and dangerous
    elsewhere."""
    common = [
        frame(f"c{i}", phash=i, priority=0.5, ts=i * 10**11, c=COMMON_CELL) for i in range(20_000)
    ]
    rare = [
        frame(f"r{i}", phash=10**6 + i, priority=0.9, ts=i * 10**11, c=SAFETY_CELL)
        for i in range(300)
    ]

    selected, report = stratified_sample(common + rare, target_size=5_000)
    kept_rare = sum(1 for f in selected if f.cell == SAFETY_CELL)

    assert kept_rare == 300, "every available safety-critical frame must be kept"
    # And the shortfall against the floor must be reported, loudly.
    assert SAFETY_CELL.key in report.safety_critical_shortfall
    assert not report.ok


def test_report_is_ok_when_safety_floors_are_met():
    rare = [
        frame(f"r{i}", phash=i, priority=0.9, ts=i * 10**11, c=SAFETY_CELL)
        for i in range(sampling_floor(SAFETY_CELL))
    ]
    _, report = stratified_sample(rare, target_size=10**6)
    assert report.ok
    assert not report.safety_critical_shortfall


def test_share_ceiling_caps_a_dominant_cell():
    common = [
        frame(f"c{i}", phash=i, priority=0.5, ts=i * 10**11, c=COMMON_CELL) for i in range(50_000)
    ]
    selected, report = stratified_sample(common, target_size=1_000, share_ceiling=0.06)
    assert len(selected) <= 60 + 1
    assert COMMON_CELL.key in report.capped


def test_uninterpretable_frames_are_never_selected():
    whiteout = cell(part=Particulate.WHITEOUT)
    frames = [frame(f"w{i}", phash=i, priority=1.0, ts=i * 10**11, c=whiteout) for i in range(50)]
    selected, _ = stratified_sample(frames, target_size=100)
    assert selected == []


# --- snapshot ----------------------------------------------------------------


ENTRIES = [
    FileEntry("b/2.parquet", 200, "b" * 64),
    FileEntry("a/1.parquet", 100, "a" * 64),
    FileEntry("c/3.parquet", 300, "c" * 64),
]


def test_merkle_root_is_order_independent():
    # ADLS listings, Delta manifests, and local filesystems return different
    # orders; the root must not depend on who looked.
    assert merkle_root(ENTRIES) == merkle_root(list(reversed(ENTRIES)))


def test_merkle_root_changes_when_content_changes():
    mutated = [FileEntry("a/1.parquet", 101, "a" * 64), *ENTRIES[1:]]
    assert merkle_root(ENTRIES) != merkle_root(mutated)


def test_merkle_root_handles_odd_node_counts():
    assert merkle_root(ENTRIES[:1]) != merkle_root(ENTRIES[:2])
    assert merkle_root([]) == merkle_root([])


def test_verify_detects_tampering():
    snap = build(
        dataset="mr1-hazard",
        version=12,
        root="abfss://snapshot/mr1-hazard/12",
        entries=ENTRIES,
        source_commit="deadbeef",
        coverage={COMMON_CELL.key: 10},
    )
    assert verify(snap, ENTRIES)
    assert not verify(snap, ENTRIES[:2])


def test_manifest_duplicates_lineage_into_the_snapshot():
    # The record must survive the AML workspace being rebuilt or the asset deleted.
    snap = build(
        dataset="mr1-hazard",
        version=12,
        root="abfss://x",
        entries=ENTRIES,
        source_commit="deadbeef",
        coverage={},
        note="add wet-decline frames",
    )
    import json

    doc = json.loads(manifest_json(snap))
    assert doc["merkle_root"] == snap.merkle_root
    assert doc["source_commit"] == "deadbeef"
    assert snap.to_tags()["immutable"] == "true"
