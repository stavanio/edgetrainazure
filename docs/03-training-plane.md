# 03: Training plane

> **Design artifact.** This describes an architecture that has not been deployed.
> Figures are design targets and planning assumptions, not measurements,
> see the status table in the [README](../README.md).

## 3.1 Labeling: teacher-student with human adjudication

Human labeling is the most expensive resource in the loop, roughly 40× the cost per frame
of every compute step combined. The design goal is therefore not "label well" but
**"label as few frames as possible, and only the ones that matter."**

The percentages below are **modeled**, used to size the labeling budget. The
auto-accept rate is the most important assumption in the entire design and the
first thing to establish against a real teacher and a real dataset.

```
/curated frame
   │
   ├─▶ teacher endpoint (AML managed online endpoint, A100)
   │     open-vocabulary detector + promptable segmenter
   │     emits: boxes, masks, class, confidence
   │
   ├─▶ deployed student (the model currently on the fleet)
   │     emits: boxes, masks, class, confidence
   │
   ▼
 routing decision
   ├─ teacher confident AND student agrees  ──▶ auto-accept   (≈71% of frames)
   ├─ teacher confident AND student differs ──▶ human review  (≈18%)  ← highest value
   ├─ teacher unconfident                   ──▶ human label   (≈9%)
   └─ safety-critical class involved        ──▶ human label, 2× redundant (≈2%)
```

Auto-accepted labels are **marked as such** in `/labeled` and carry the teacher version.
They are never used alone to evaluate; the golden set is 100% human, always.

The teacher is large, slow, and expensive per frame. That is fine: it runs offline, in
batch, on a schedule, and it never ships to a robot. Implementation in
[`src/edgeforge/labeling/autolabel_teacher.py`](../src/edgeforge/labeling/autolabel_teacher.py).

Human work happens in **Azure ML Data Labeling** projects with ML-assisted pre-labels
seeded from the teacher, so annotators correct rather than draw. Labeler agreement is
tracked per annotator per class; a Cohen's κ below 0.75 on the calibration set pulls that
annotator's recent work back into the queue.

## 3.2 Synthetic data

Real data cannot cover the tail. There is no ethical way to collect a thousand examples of
*a person in the wrong place behind a moving haul vehicle in a dust cloud*, and that is
exactly the case that must not fail.

[`sim/`](../sim) drives NVIDIA Isaac Sim headless on an Azure Batch spot pool
(`Standard_NV36ads_A10_v5`). Each job renders a scenario from
[`sim/scenarios.yaml`](../sim/scenarios.yaml) with domain randomization over:

- Illumination: headlamp count/aim/colour temp, ambient, glare sources
- Airborne particulate: density, size distribution, motion
- Surface: wet/dry, spillage, rutting, standing water reflectance
- Geometry: drift cross-section, back height, rib irregularity, junction topology
- Actors: personnel with varied PPE and pose, other machines, parked equipment
- Sensor: exposure, gain, motion blur, lens contamination, LiDAR dropout and returns in dust
- Extrinsics: ±2° mount perturbation to prevent the model memorising a rig

Output is pixel-perfect ground truth (masks, depth, instance IDs, 3D boxes) with zero
labeling cost. It lands in `/labeled` tagged `source=sim` and is **never** allowed into the
evaluation set.

**The sim-to-real gap is real and is managed, not ignored.** Synthetic data is used for
pre-training and for tail-class oversampling only. The working assumption, taken from
published results on comparable perception workloads: synthetic pre-training buys
single-digit mAP on rare classes and materially reduces the real frames needed to reach a
target, while a model trained on synthetic alone degrades sharply on real degraded-visibility
imagery. The `sim_ratio` sweep exists precisely because this assumption has to be
established per workload rather than inherited. See the `sim_ratio` sweep in
[`pipelines/aml/pipeline-train-perception.yml`](../pipelines/aml/pipeline-train-perception.yml).

## 3.3 Training

Azure ML v2 pipeline jobs, four stages, MLflow-tracked throughout.

| Stage | Compute | What |
|---|---|---|
| `pretrain` | 8× A100 (`Standard_NC96ads_A100_v4`) | Teacher-capacity backbone on synthetic + auto-labeled real |
| `finetune` | 8× A100 | Human-labeled real only, low LR, class-balanced sampler |
| `distill` | 4× A100 | Student (edge-sized) learns from teacher logits + hard labels |
| `evaluate` | 1× A100 | Golden set, slice metrics, closed-loop replay, gates |

Distributed via PyTorch DDP with NCCL over the InfiniBand-backed ND-series when available;
sweeps use low-priority nodes because a preempted trial is cheap and an interrupted final
run is not.

### The student is the product

`hazard-seg` ships as a ~9 M-parameter encoder-decoder with a shared backbone across all
four heads; one backbone pass, four heads, because 45 ms p99 at 40 W does not permit four
independent networks. The multi-head arrangement is also a regulariser: personnel detection
and drivable-surface estimation constrain each other in useful ways.

Distillation targets, in order of weight: teacher soft logits, hard human labels, feature-map
alignment at two scales. Implementation in
[`src/edgeforge/training/train_perception.py`](../src/edgeforge/training/train_perception.py).

### Reproducibility

Every job pins: snapshot Merkle root, git SHA of this repo, environment image digest (not
tag), CUDA/cuDNN versions, and all seeds. `deterministic=True` costs throughput, budgeted
at ~12%, and is enabled for `finetune` and `distill`, disabled for sweeps. An AML job can be re-run from its
recorded inputs and lands within tolerance; this is checked weekly by a canary re-run, not
assumed.

## 3.4 Evaluation

mAP is necessary and nowhere near sufficient. A model can gain mAP fleet-wide while getting
worse at the one thing that hurts someone.

### Three tiers of gate

[`src/edgeforge/evaluation/gates.py`](../src/edgeforge/evaluation/gates.py)

**Tier 1, aggregate.** Overall mAP, mIoU, and calibration (expected calibration error) on
the human-only golden set. Must not regress beyond tolerance vs. the incumbent.

**Tier 2, slices.** The same metrics computed per taxonomy cell. **No slice may regress
more than 1.5 points, regardless of aggregate movement.** This is the gate that catches
"improved overall by getting better at easy frames." Slices that matter most:
`personnel_present × high_dust`, `personnel_present × low_light`, `wet_surface × decline`.

**Tier 3, closed-loop.** The model is dropped into a replay harness over ~2,000 recorded
and simulated scenarios and scored on *decision* outcomes, not pixels:

| Metric | Meaning | Gate |
|---|---|---|
| Time-to-detect | First frame a hazard is detected, relative to ground truth entry | No regression |
| Time-to-brake margin | Slack between detection and required stopping distance | ≥ incumbent |
| False-stop rate | Nuisance stops per operating hour | ≤ incumbent × 1.05 |
| Missed-personnel rate | Any frame with a person undetected inside the safety envelope | **Hard zero** |
| Track stability | ID switches per 1,000 frames | ≤ incumbent × 1.10 |

Missed-personnel is not a threshold; it is an absolute. One instance blocks promotion and
the frame becomes a permanent golden-set member.

### Golden set discipline

- 100% human-labeled, 2× redundant, adjudicated on disagreement
- Frozen; grows only by addition, never revision, never deletion
- Every historical failure lands in it permanently
- Physically separated: a distinct storage container with a distinct managed identity that
  training compute cannot read. Test-set leakage is prevented by IAM, not by convention.

## 3.5 Promotion

Passing gates does not deploy anything. It registers the model in the **AML Registry** with
stage `Evaluated` and attaches:

- Model card (auto-generated from gate results,
  [`src/edgeforge/packaging/model_card.py`](../src/edgeforge/packaging/model_card.py))
- Full lineage: snapshot root → job → environment digest → metrics
- Slice table and closed-loop scores versus incumbent
- Signed evaluation attestation

Promotion `Evaluated → Approved` is a human decision in `prod`, requiring two approvers
from different teams, recorded in the registry. From `Approved`, the edge plane takes over.
