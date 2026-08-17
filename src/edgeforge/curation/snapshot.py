"""Snapshot: /curated -> /snapshot, and the AML data asset that names it.

A training run never reads a live table. It reads a frozen, content-addressed
Delta deep clone. The reason is not tidiness -- it is that when a field incident
is investigated eighteen months later, "what exactly did this model learn from"
must be a query, not an archaeology project.

The Merkle root over the sorted file list is what makes that claim checkable:
recompute it against the snapshot directory and you have shown the bytes have
not moved.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str  # relative to the snapshot root
    size: int
    sha256: str


@dataclass(slots=True)
class Snapshot:
    dataset: str
    version: int
    root: str  # abfss:// path
    merkle_root: str
    file_count: int
    total_bytes: int
    created_utc: str
    source_commit: str  # git SHA of this repo at snapshot time
    coverage: dict[str, int] = field(default_factory=dict)
    note: str = ""

    @property
    def asset_name(self) -> str:
        return f"{self.dataset}:{self.version}"

    def to_tags(self) -> dict[str, str]:
        """Tags attached to the AML data asset. These are the lineage record."""
        return {
            "merkle_root": self.merkle_root,
            "file_count": str(self.file_count),
            "total_bytes": str(self.total_bytes),
            "created_utc": self.created_utc,
            "source_commit": self.source_commit,
            "immutable": "true",
            "note": self.note[:250],
        }


def merkle_root(entries: Sequence[FileEntry]) -> str:
    """Binary Merkle root over (path, size, sha256), sorted by path.

    Sorting by path makes the root independent of listing order, which differs
    between ADLS listings, Delta manifests, and local filesystems. Without it
    the same snapshot hashes differently depending on who looked at it.
    """
    if not entries:
        return hashlib.sha256(b"").hexdigest()

    leaves = [
        hashlib.sha256(f"{e.path}\0{e.size}\0{e.sha256}".encode()).digest()
        for e in sorted(entries, key=lambda e: e.path)
    ]

    while len(leaves) > 1:
        if len(leaves) % 2:
            leaves.append(leaves[-1])  # duplicate the odd tail node
        leaves = [
            hashlib.sha256(leaves[i] + leaves[i + 1]).digest() for i in range(0, len(leaves), 2)
        ]
    return leaves[0].hex()


def build(
    *,
    dataset: str,
    version: int,
    root: str,
    entries: Sequence[FileEntry],
    source_commit: str,
    coverage: dict[str, int],
    note: str = "",
) -> Snapshot:
    snap = Snapshot(
        dataset=dataset,
        version=version,
        root=root,
        merkle_root=merkle_root(entries),
        file_count=len(entries),
        total_bytes=sum(e.size for e in entries),
        created_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        source_commit=source_commit,
        coverage=coverage,
        note=note,
    )
    log.info(
        "snapshot %s: %d files, %.1f GiB, merkle=%s",
        snap.asset_name,
        snap.file_count,
        snap.total_bytes / 2**30,
        snap.merkle_root[:16],
    )
    return snap


def verify(snap: Snapshot, entries: Sequence[FileEntry]) -> bool:
    """Recompute the root against what is actually on disk.

    Run by the annual lineage audit (docs/08-runbook.md §8.4). This is the check
    that the whole reproducibility claim rests on, so it is worth running before
    anyone asks.
    """
    actual = merkle_root(entries)
    if actual != snap.merkle_root:
        log.error(
            "snapshot %s FAILED verification: expected %s, got %s",
            snap.asset_name,
            snap.merkle_root,
            actual,
        )
        return False
    log.info("snapshot %s verified", snap.asset_name)
    return True


def manifest_json(snap: Snapshot) -> str:
    """Written to <root>/_manifest.json alongside the data.

    Duplicating the lineage into the snapshot itself means the record survives
    even if the AML workspace is rebuilt or the asset is deleted.
    """
    return json.dumps(
        {
            "dataset": snap.dataset,
            "version": snap.version,
            "merkle_root": snap.merkle_root,
            "file_count": snap.file_count,
            "total_bytes": snap.total_bytes,
            "created_utc": snap.created_utc,
            "source_commit": snap.source_commit,
            "coverage": snap.coverage,
            "note": snap.note,
            "schema_version": 1,
        },
        indent=2,
        sort_keys=True,
    )


__all__ = ["FileEntry", "Snapshot", "build", "manifest_json", "merkle_root", "verify"]
