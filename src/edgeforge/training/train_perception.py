"""Multi-head perception training: pretrain -> finetune -> distill.

The thing that ships is the *student*: a ~9M-parameter shared-backbone network
that must run four heads in 45 ms p99 inside a 40 W budget on an Orin AGX, while
SLAM, planning, and logging contend for the same SoC. Every choice here serves
that constraint.

Why one backbone and four heads rather than four networks: four independent
networks do not fit the latency budget, and the heads regularise each other --
drivable-surface estimation and personnel detection constrain one another in
ways that measurably help both.

Stages:
    pretrain   teacher-capacity backbone on synthetic + auto-labeled real
    finetune   human-labeled real only, low LR, class-balanced sampler
    distill    student learns from teacher logits + hard labels + features

Run as an AML command job; see pipelines/aml/pipeline-train-perception.yml.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

import mlflow
import numpy as np
import torch
import torch.distributed as dist
import torch.nn.functional as F
from torch import Tensor, nn
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import DataLoader

log = logging.getLogger("edgeforge.train")

HEADS = ("hazard_seg", "drivable_surface", "personnel_det", "equipment_anomaly")


class Stage(StrEnum):
    PRETRAIN = "pretrain"
    FINETUNE = "finetune"
    DISTILL = "distill"


@dataclass(slots=True)
class Config:
    stage: Stage
    snapshot_uri: str
    output_dir: str
    teacher_checkpoint: str | None = None

    epochs: int = 30
    batch_size: int = 16  # per device
    lr: float = 3e-4
    weight_decay: float = 0.05
    warmup_steps: int = 1_000
    grad_clip: float = 1.0
    amp_dtype: str = "bf16"

    # Fraction of each batch drawn from simulation. Sweeping this is worth more
    # than sweeping the learning rate: measured on this workload, synthetic
    # pretraining buys ~+6 mAP on rare classes and roughly halves the real frames
    # needed to hit a target -- but a model trained on synthetic alone loses 20+
    # mAP the first time it meets real dust.
    sim_ratio: float = 0.4

    # Distillation mixing. Feature alignment at two scales matters more than the
    # logit term for the small student; dropping it costs ~2 mAP.
    distill_logit_weight: float = 1.0
    distill_hard_weight: float = 0.5
    distill_feature_weight: float = 0.3
    distill_temperature: float = 3.0

    seed: int = 1337
    # Determinism costs ~12% throughput. On for the runs that produce shippable
    # weights, off for sweeps where a preempted trial is cheap anyway.
    deterministic: bool = True


# --- reproducibility ---------------------------------------------------------


def set_determinism(seed: int, deterministic: bool) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    if deterministic:
        torch.use_deterministic_algorithms(True, warn_only=True)
        torch.backends.cudnn.benchmark = False
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    else:
        torch.backends.cudnn.benchmark = True


# --- distributed -------------------------------------------------------------


def init_distributed() -> tuple[int, int, int]:
    """Returns (rank, world_size, local_rank). Single-process outside AML too."""
    if "RANK" not in os.environ:
        return 0, 1, 0
    rank = int(os.environ["RANK"])
    world = int(os.environ["WORLD_SIZE"])
    local = int(os.environ.get("LOCAL_RANK", rank % max(1, torch.cuda.device_count())))
    dist.init_process_group(backend="nccl", init_method="env://")
    torch.cuda.set_device(local)
    return rank, world, local


def is_primary(rank: int) -> bool:
    return rank == 0


# --- model -------------------------------------------------------------------


class SharedBackbone(nn.Module):
    """Depthwise-separable encoder. Width chosen to fit the latency budget.

    `width` is the single knob that trades accuracy for milliseconds. The student
    ships at width=32 (~9M params across all heads); the teacher trains at
    width=96 and never leaves the cloud.
    """

    def __init__(self, width: int = 32, in_channels: int = 3) -> None:
        super().__init__()
        w = width
        self.stem = nn.Sequential(
            nn.Conv2d(in_channels, w, 3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(w),
            nn.SiLU(inplace=True),
        )
        self.stages = nn.ModuleList([self._block(w * 2**i, w * 2 ** (i + 1)) for i in range(4)])
        self.out_channels = [w * 2 ** (i + 1) for i in range(4)]

    @staticmethod
    def _block(cin: int, cout: int) -> nn.Module:
        return nn.Sequential(
            nn.Conv2d(cin, cin, 3, stride=2, padding=1, groups=cin, bias=False),
            nn.BatchNorm2d(cin),
            nn.SiLU(inplace=True),
            nn.Conv2d(cin, cout, 1, bias=False),
            nn.BatchNorm2d(cout),
            nn.SiLU(inplace=True),
        )

    def forward(self, x: Tensor) -> list[Tensor]:
        x = self.stem(x)
        feats: list[Tensor] = []
        for stage in self.stages:
            x = stage(x)
            feats.append(x)
        return feats


class SegHead(nn.Module):
    def __init__(self, channels: list[int], num_classes: int) -> None:
        super().__init__()
        self.lateral = nn.ModuleList(nn.Conv2d(c, 64, 1) for c in channels)
        self.fuse = nn.Sequential(
            nn.Conv2d(64, 64, 3, padding=1, bias=False),
            nn.BatchNorm2d(64),
            nn.SiLU(inplace=True),
        )
        self.classifier = nn.Conv2d(64, num_classes, 1)

    def forward(self, feats: list[Tensor]) -> Tensor:
        x = self.lateral[-1](feats[-1])
        for lat, f in zip(reversed(self.lateral[:-1]), reversed(feats[:-1]), strict=True):
            x = F.interpolate(x, size=f.shape[-2:], mode="nearest") + lat(f)
        return self.classifier(self.fuse(x))


class DetHead(nn.Module):
    """Anchor-free centre/size/objectness. Kept simple: NMS runs on the CPU side
    of the edge module, so the head must not depend on GPU-side postprocessing."""

    def __init__(self, channels: list[int], num_classes: int) -> None:
        super().__init__()
        c = channels[-2]
        self.tower = nn.Sequential(
            nn.Conv2d(c, c, 3, padding=1, bias=False),
            nn.BatchNorm2d(c),
            nn.SiLU(inplace=True),
        )
        self.cls = nn.Conv2d(c, num_classes, 1)
        self.reg = nn.Conv2d(c, 4, 1)
        self.obj = nn.Conv2d(c, 1, 1)

    def forward(self, feats: list[Tensor]) -> dict[str, Tensor]:
        x = self.tower(feats[-2])
        return {"cls": self.cls(x), "reg": self.reg(x), "obj": self.obj(x)}


class PerceptionNet(nn.Module):
    def __init__(self, width: int = 32, num_classes: dict[str, int] | None = None) -> None:
        super().__init__()
        nc = num_classes or {
            "hazard_seg": 9,
            "drivable_surface": 3,
            "personnel_det": 3,
            "equipment_anomaly": 5,
        }
        self.backbone = SharedBackbone(width)
        ch = self.backbone.out_channels
        self.hazard_seg = SegHead(ch, nc["hazard_seg"])
        self.drivable_surface = SegHead(ch, nc["drivable_surface"])
        self.personnel_det = DetHead(ch, nc["personnel_det"])
        self.equipment_anomaly = DetHead(ch, nc["equipment_anomaly"])

    def forward(self, x: Tensor, return_features: bool = False):
        feats = self.backbone(x)
        out = {
            "hazard_seg": self.hazard_seg(feats),
            "drivable_surface": self.drivable_surface(feats),
            "personnel_det": self.personnel_det(feats),
            "equipment_anomaly": self.equipment_anomaly(feats),
        }
        if return_features:
            out["_features"] = feats
        return out


# --- losses ------------------------------------------------------------------


def seg_loss(logits: Tensor, target: Tensor, weight: Tensor | None = None) -> Tensor:
    logits = F.interpolate(logits, size=target.shape[-2:], mode="bilinear", align_corners=False)
    ce = F.cross_entropy(logits, target, reduction="none")
    if weight is not None:
        ce = ce * weight.view(-1, 1, 1)
    return ce.mean()


def det_loss(
    pred: dict[str, Tensor], target: dict[str, Tensor], weight: Tensor | None = None
) -> Tensor:
    obj = F.binary_cross_entropy_with_logits(pred["obj"], target["obj"], reduction="none")
    cls = F.binary_cross_entropy_with_logits(pred["cls"], target["cls"], reduction="none")
    # Regression only where an object exists; empty frames must not pull boxes.
    mask = target["obj"] > 0.5
    reg = F.smooth_l1_loss(pred["reg"], target["reg"], reduction="none") * mask
    total = obj.mean(dim=(1, 2, 3)) + cls.mean(dim=(1, 2, 3)) + reg.mean(dim=(1, 2, 3))
    if weight is not None:
        total = total * weight
    return total.mean()


def distillation_loss(
    student_out: dict, teacher_out: dict, cfg: Config
) -> tuple[Tensor, dict[str, float]]:
    """Logit KD + feature alignment. Hard-label term is added by the caller."""
    T = cfg.distill_temperature
    logit_terms: list[Tensor] = []
    for head in ("hazard_seg", "drivable_surface"):
        s, t = student_out[head], teacher_out[head]
        if s.shape[-2:] != t.shape[-2:]:
            s = F.interpolate(s, size=t.shape[-2:], mode="bilinear", align_corners=False)
        logit_terms.append(
            F.kl_div(
                F.log_softmax(s / T, dim=1),
                F.softmax(t / T, dim=1),
                reduction="batchmean",
            )
            * (T * T)
        )
    logit = torch.stack(logit_terms).mean()

    # Feature alignment at two scales, channel-normalised so the student's
    # narrower backbone is not penalised for having fewer channels.
    feat_terms: list[Tensor] = []
    for s, t in zip(student_out["_features"][-2:], teacher_out["_features"][-2:], strict=True):
        s_n = F.normalize(s.mean(dim=1, keepdim=True), dim=(2, 3))
        t_n = F.normalize(t.mean(dim=1, keepdim=True), dim=(2, 3))
        if s_n.shape[-2:] != t_n.shape[-2:]:
            s_n = F.interpolate(s_n, size=t_n.shape[-2:], mode="bilinear", align_corners=False)
        feat_terms.append(F.mse_loss(s_n, t_n))
    feature = torch.stack(feat_terms).mean()

    total = cfg.distill_logit_weight * logit + cfg.distill_feature_weight * feature
    return total, {"kd_logit": float(logit), "kd_feature": float(feature)}


# --- training loop -----------------------------------------------------------


def build_optimizer(model: nn.Module, cfg: Config) -> torch.optim.Optimizer:
    # No weight decay on norms and biases -- decaying them measurably hurts the
    # small student, which has little capacity to spare.
    decay, no_decay = [], []
    for name, p in model.named_parameters():
        if not p.requires_grad:
            continue
        (no_decay if p.ndim <= 1 or name.endswith(".bias") else decay).append(p)
    return torch.optim.AdamW(
        [
            {"params": decay, "weight_decay": cfg.weight_decay},
            {"params": no_decay, "weight_decay": 0.0},
        ],
        lr=cfg.lr,
        betas=(0.9, 0.95),
    )


def lr_at(step: int, cfg: Config, total_steps: int) -> float:
    if step < cfg.warmup_steps:
        return cfg.lr * step / max(1, cfg.warmup_steps)
    progress = (step - cfg.warmup_steps) / max(1, total_steps - cfg.warmup_steps)
    return 0.5 * cfg.lr * (1.0 + np.cos(np.pi * min(1.0, progress)))


def train(
    cfg: Config,
    train_loader: DataLoader,
    val_loader: DataLoader | None = None,
) -> Path:
    rank, world, local = init_distributed()
    set_determinism(cfg.seed + rank, cfg.deterministic)
    device = torch.device(f"cuda:{local}" if torch.cuda.is_available() else "cpu")

    width = 96 if cfg.stage is not Stage.DISTILL else 32
    model = PerceptionNet(width=width).to(device)

    teacher: nn.Module | None = None
    if cfg.stage is Stage.DISTILL:
        if not cfg.teacher_checkpoint:
            raise ValueError("distill stage requires --teacher-checkpoint")
        teacher = PerceptionNet(width=96).to(device)
        teacher.load_state_dict(torch.load(cfg.teacher_checkpoint, map_location=device))
        teacher.eval()
        for p in teacher.parameters():
            p.requires_grad_(False)

    if world > 1:
        model = DistributedDataParallel(
            model, device_ids=[local] if device.type == "cuda" else None
        )

    opt = build_optimizer(model, cfg)
    amp_dtype = torch.bfloat16 if cfg.amp_dtype == "bf16" else torch.float16
    scaler = torch.amp.GradScaler("cuda", enabled=amp_dtype is torch.float16)

    total_steps = cfg.epochs * len(train_loader)
    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if is_primary(rank):
        mlflow.log_params({f"cfg.{k}": v for k, v in asdict(cfg).items()})
        mlflow.log_param("backbone_width", width)
        mlflow.log_param("world_size", world)

    step = 0
    for epoch in range(cfg.epochs):
        model.train()
        if hasattr(train_loader.sampler, "set_epoch"):
            train_loader.sampler.set_epoch(epoch)  # type: ignore[union-attr]

        for batch in train_loader:
            images = batch["image"].to(device, non_blocking=True)
            # Per-frame label-provenance weights: auto-accepted labels carry less
            # weight than human ones, so teacher errors do not become permanent
            # student errors. See labeling/autolabel_teacher.label_confidence_weights.
            weights = batch.get("label_weight")
            weights = weights.to(device) if weights is not None else None

            for g in opt.param_groups:
                g["lr"] = lr_at(step, cfg, total_steps)

            with torch.autocast("cuda", dtype=amp_dtype, enabled=device.type == "cuda"):
                out = model(images, return_features=cfg.stage is Stage.DISTILL)

                loss = images.new_zeros(())
                metrics: dict[str, float] = {}

                loss = loss + seg_loss(out["hazard_seg"], batch["hazard_seg"].to(device), weights)
                loss = loss + seg_loss(
                    out["drivable_surface"], batch["drivable_surface"].to(device), weights
                )
                for head in ("personnel_det", "equipment_anomaly"):
                    tgt = {k: v.to(device) for k, v in batch[head].items()}
                    loss = loss + det_loss(out[head], tgt, weights)

                if teacher is not None:
                    with torch.no_grad():
                        t_out = teacher(images, return_features=True)
                    kd, kd_metrics = distillation_loss(out, t_out, cfg)
                    loss = cfg.distill_hard_weight * loss + kd
                    metrics.update(kd_metrics)

            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.grad_clip)
            scaler.step(opt)
            scaler.update()

            if is_primary(rank) and step % 50 == 0:
                mlflow.log_metrics(
                    {"train/loss": float(loss), "train/lr": opt.param_groups[0]["lr"], **metrics},
                    step=step,
                )
                log.info("epoch %d step %d loss %.4f", epoch, step, float(loss))
            step += 1

        if val_loader is not None and is_primary(rank):
            val = validate(model, val_loader, device)
            mlflow.log_metrics({f"val/{k}": v for k, v in val.items()}, step=step)
            log.info("epoch %d validation %s", epoch, json.dumps(val, sort_keys=True))

    if is_primary(rank):
        state = model.module.state_dict() if world > 1 else model.state_dict()
        ckpt = out_dir / f"{cfg.stage.value}.pt"
        torch.save(state, ckpt)
        (out_dir / "config.json").write_text(json.dumps(asdict(cfg), indent=2, default=str))
        mlflow.log_artifact(str(ckpt))
        log.info("wrote %s", ckpt)
    else:
        ckpt = out_dir / f"{cfg.stage.value}.pt"

    if world > 1:
        dist.barrier()
        dist.destroy_process_group()
    return ckpt


@torch.no_grad()
def validate(model: nn.Module, loader: DataLoader, device: torch.device) -> dict[str, float]:
    model.eval()
    losses: list[float] = []
    for batch in loader:
        images = batch["image"].to(device, non_blocking=True)
        out = model(images)
        loss = seg_loss(out["hazard_seg"], batch["hazard_seg"].to(device))
        losses.append(float(loss))
    model.train()
    return {"loss": float(np.mean(losses)) if losses else float("nan")}


# --- entry point -------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(description="edgeforge perception training")
    p.add_argument("--stage", type=Stage, choices=list(Stage), required=True)
    p.add_argument("--snapshot-uri", required=True, help="azureml:// data asset URI")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--teacher-checkpoint")
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--sim-ratio", type=float, default=0.4)
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--no-deterministic", dest="deterministic", action="store_false")
    a = p.parse_args(argv)
    return Config(
        stage=a.stage,
        snapshot_uri=a.snapshot_uri,
        output_dir=a.output_dir,
        teacher_checkpoint=a.teacher_checkpoint,
        epochs=a.epochs,
        batch_size=a.batch_size,
        lr=a.lr,
        sim_ratio=a.sim_ratio,
        seed=a.seed,
        deterministic=a.deterministic,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = parse_args()
    from edgeforge.training.datamodule import build_loaders  # local import: heavy

    train_loader, val_loader = build_loaders(cfg)
    train(cfg, train_loader, val_loader)
