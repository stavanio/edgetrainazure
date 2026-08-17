"""Dataset and loaders over a frozen snapshot.

Two things here are load-bearing and easy to get wrong elsewhere:

  1. `sim_ratio` mixing happens at the *sampler*, not by concatenating datasets.
     Concatenation makes the synthetic fraction drift as the real set grows,
     which silently changes the recipe between runs and makes the sim_ratio
     sweep meaningless.

  2. Preprocessing lives in `PreprocessSpec`, which is serialised into the edge
     bundle verbatim. Train/serve preprocessing skew -- a different resize
     interpolation, a swapped colour order, a normalisation constant that
     drifted -- is the most common cause of "great in eval, mediocre in the
     field". Making the spec an artifact rather than code on both sides removes
     the failure mode entirely.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset, Sampler
from torch.utils.data.distributed import DistributedSampler

from edgeforge.taxonomy import Cell

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreprocessSpec:
    """Shipped verbatim to the robot as config/preprocess.json."""

    width: int = 960
    height: int = 600
    resize: str = "bilinear"  # must match the edge implementation exactly
    letterbox: bool = True
    letterbox_value: int = 114
    colour_order: str = "rgb"
    mean: tuple[float, float, float] = (0.485, 0.456, 0.406)
    std: tuple[float, float, float] = (0.229, 0.224, 0.225)
    scale: float = 1.0 / 255.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> PreprocessSpec:
        d = json.loads(text)
        d["mean"] = tuple(d["mean"])
        d["std"] = tuple(d["std"])
        return cls(**d)


@dataclass(slots=True)
class Record:
    frame_id: str
    image_path: str
    cell: Cell
    source: str  # "real" | "sim"
    label_weight: float  # from labeling provenance
    label_paths: dict[str, str]


class SnapshotDataset(Dataset):
    """Reads a frozen snapshot. Never a live table -- see curation/snapshot.py."""

    def __init__(
        self,
        records: Sequence[Record],
        spec: PreprocessSpec,
        *,
        augment: bool = True,
        rng_seed: int = 0,
    ) -> None:
        self.records = list(records)
        self.spec = spec
        self.augment = augment
        self._rng = np.random.default_rng(rng_seed)

    def __len__(self) -> int:
        return len(self.records)

    @property
    def sim_indices(self) -> list[int]:
        return [i for i, r in enumerate(self.records) if r.source == "sim"]

    @property
    def real_indices(self) -> list[int]:
        return [i for i, r in enumerate(self.records) if r.source == "real"]

    def __getitem__(self, idx: int) -> dict[str, Any]:
        rec = self.records[idx]
        image = self._load_image(rec.image_path)
        labels = self._load_labels(rec)

        if self.augment:
            image, labels = self._augment(image, labels)

        return {
            "frame_id": rec.frame_id,
            "image": torch.from_numpy(image),
            "label_weight": torch.tensor(rec.label_weight, dtype=torch.float32),
            "cell": rec.cell.key,
            **labels,
        }

    # -- IO stubs: wire to the ADLS-backed reader in the AML environment ------

    def _load_image(self, path: str) -> np.ndarray:
        raise NotImplementedError("bind to the snapshot reader in the AML environment")

    def _load_labels(self, rec: Record) -> dict[str, Any]:
        raise NotImplementedError("bind to the snapshot reader in the AML environment")

    def _augment(self, image: np.ndarray, labels: dict) -> tuple[np.ndarray, dict]:
        """Photometric only, plus horizontal flip.

        Deliberately conservative. Aggressive geometric augmentation teaches the
        model that the camera mount moves, which it does not -- and the
        mount-perturbation randomisation already happens in simulation, where the
        ground truth moves with it. Doing it here would decorrelate image and
        label.
        """
        if self._rng.random() < 0.5:
            image = image[:, ::-1].copy()
            for k, v in labels.items():
                if isinstance(v, np.ndarray) and v.ndim >= 2:
                    labels[k] = v[..., ::-1].copy()

        gain = float(self._rng.uniform(0.85, 1.15))
        bias = float(self._rng.uniform(-0.05, 0.05))
        image = np.clip(image * gain + bias, 0.0, 1.0)
        return image, labels


class SimRatioSampler(Sampler[int]):
    """Draws each batch with a fixed synthetic fraction.

    Holding the ratio at the sampler keeps the recipe stable as the real dataset
    grows, which is what makes `sim_ratio` a meaningful thing to sweep.
    """

    def __init__(
        self,
        dataset: SnapshotDataset,
        *,
        sim_ratio: float,
        batch_size: int,
        num_batches: int,
        seed: int = 0,
    ) -> None:
        self.sim = dataset.sim_indices
        self.real = dataset.real_indices
        self.sim_per_batch = round(sim_ratio * batch_size)
        self.real_per_batch = batch_size - self.sim_per_batch
        self.num_batches = num_batches
        self.rng = np.random.default_rng(seed)

        if self.sim_per_batch and not self.sim:
            log.warning("sim_ratio > 0 but snapshot contains no synthetic frames")
            self.real_per_batch, self.sim_per_batch = batch_size, 0

    def __len__(self) -> int:
        return self.num_batches * (self.sim_per_batch + self.real_per_batch)

    def __iter__(self) -> Iterator[int]:
        for _ in range(self.num_batches):
            batch: list[int] = []
            if self.real_per_batch and self.real:
                batch += list(
                    self.rng.choice(
                        self.real, self.real_per_batch, replace=len(self.real) < self.real_per_batch
                    )
                )
            if self.sim_per_batch and self.sim:
                batch += list(
                    self.rng.choice(
                        self.sim, self.sim_per_batch, replace=len(self.sim) < self.sim_per_batch
                    )
                )
            self.rng.shuffle(batch)
            yield from (int(i) for i in batch)


def load_records(snapshot_uri: str) -> list[Record]:
    """Read the snapshot manifest into Record objects.

    The manifest is written by curation.snapshot; reading it (rather than
    listing the directory) is what ties the loader to the verified Merkle root.
    """
    manifest = Path(snapshot_uri) / "_manifest.json"
    raise NotImplementedError(f"bind to the ADLS snapshot reader; expected manifest at {manifest}")


def build_loaders(cfg) -> tuple[DataLoader, DataLoader | None]:
    """Construct train/val loaders for a training Config."""
    spec = PreprocessSpec()
    records = load_records(cfg.snapshot_uri)

    # Validation is a held-out split of the snapshot -- NOT the golden set. The
    # golden set lives in a separate storage account that training compute has no
    # role assignment on at all (infra/storage.tf), so this cannot accidentally
    # become test-set leakage.
    split = int(0.95 * len(records))
    train_ds = SnapshotDataset(records[:split], spec, augment=True, rng_seed=cfg.seed)
    val_ds = SnapshotDataset(records[split:], spec, augment=False, rng_seed=cfg.seed)

    steps_per_epoch = max(1, len(train_ds) // cfg.batch_size)
    sampler: Sampler[int] = SimRatioSampler(
        train_ds,
        sim_ratio=cfg.sim_ratio if cfg.stage.value == "pretrain" else 0.0,
        batch_size=cfg.batch_size,
        num_batches=steps_per_epoch,
        seed=cfg.seed,
    )

    train_loader = DataLoader(
        train_ds,
        batch_size=cfg.batch_size,
        sampler=sampler,
        num_workers=8,
        pin_memory=True,
        drop_last=True,
        persistent_workers=True,
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=cfg.batch_size,
        sampler=DistributedSampler(val_ds, shuffle=False)
        if torch.distributed.is_initialized()
        else None,
        num_workers=4,
        pin_memory=True,
    )
    return train_loader, val_loader


__all__ = [
    "PreprocessSpec",
    "Record",
    "SimRatioSampler",
    "SnapshotDataset",
    "build_loaders",
    "load_records",
]
