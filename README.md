# edgetrainazure — `edgeforge`

**An end-to-end AI training and deployment pipeline on Microsoft Azure for a fleet of
autonomous, edge-inferencing field robots.**

The reference workload is a **Class-4 subterranean haulage & inspection robot** (`MR-1`)
operating in an underground hard-rock mine: intermittent or absent connectivity, dust,
low light, no GNSS, safety-critical proximity to personnel, and a hard on-board compute
budget. The platform is domain-agnostic — the same pipeline runs unchanged for any
complex robot whose perception stack must be trained in the cloud and executed at the
edge.

This repository is the Azure counterpart to the AWS reference ecosystem
(S3/Glue → Ground Truth → SageMaker → Model Registry → Greengrass OTA → Jetson).
See [`docs/05-aws-to-azure-mapping.md`](docs/05-aws-to-azure-mapping.md) for the
service-by-service translation and the places where the mapping is *not* one-to-one.

---

## 1. The loop

`edgeforge` is a **data flywheel**, not a linear pipeline. Robots produce the data that
trains the models that go back onto the robots that produce better data.

```
                       ┌──────────────────────────────────────────────┐
                       │                                              │
                       ▼                                              │
   ┌───────────┐   ┌────────┐   ┌──────────┐   ┌───────────┐   ┌──────────────┐
   │  INGEST   │──▶│ CURATE │──▶│  LABEL   │──▶│   TRAIN   │──▶│   EVALUATE   │
   │ IoT Hub / │   │ Databr │   │ AML Data │   │ AML GPU   │   │ golden set + │
   │ ADLS/Box  │   │ + Delta│   │ Labeling │   │ + MLflow  │   │ closed-loop  │
   └───────────┘   └────────┘   └──────────┘   └───────────┘   └──────┬───────┘
        ▲                                                             │ gate
        │                                                             ▼
   ┌────┴──────┐   ┌────────────┐   ┌─────────────┐   ┌──────────────────────┐
   │ ACTIVE    │◀──│  FLEET     │◀──│  ROLLOUT    │◀──│  OPTIMIZE + PACKAGE  │
   │ LEARNING  │   │ TELEMETRY  │   │ canary→prod │   │ ONNX→TensorRT INT8   │
   │ uploads   │   │ + drift    │   │ IoT Edge    │   │ signed OCI bundle    │
   └───────────┘   └────────────┘   └─────────────┘   └──────────────────────┘
```

Nine stages, each independently runnable, each gated:

| # | Stage | Primary Azure service | Entry point |
|---|-------|----------------------|-------------|
| 1 | Ingest | IoT Hub, ADLS Gen2, Data Box | `src/edge_modules/curator/` |
| 2 | Curate | Databricks (Delta), AML data jobs | `src/edgeforge/curation/` |
| 3 | Label | AML Data Labeling + teacher auto-label | `src/edgeforge/labeling/` |
| 4 | Synthesize | Isaac Sim on Azure Batch GPU | `sim/` |
| 5 | Train | AML command/sweep jobs, MLflow | `src/edgeforge/training/` |
| 6 | Evaluate | AML jobs + closed-loop sim replay | `src/edgeforge/evaluation/` |
| 7 | Optimize | ONNX Runtime, TensorRT INT8 on HIL rack | `src/edgeforge/optimize/` |
| 8 | Package | ACR arm64 multi-arch, Notation signing | `src/edgeforge/packaging/` |
| 9 | Roll out | IoT Edge layered deployments, rings | `deploy/rollout/` |

---

## 2. Why each choice

**Ingest is store-and-forward, not streaming.** A robot underground has no link. The
`curator` edge module writes MCAP shards to a local ring buffer and only uploads when it
surfaces onto site Wi-Fi. High-value shards (safety events, disengagements, high-entropy
frames) are uploaded first, ordered by a priority score computed *on the robot*. Bulk
history moves by Azure Data Box when a site accumulates faster than its uplink drains.

**Curation is a gate, not a transform.** ~85% of raw field frames are near-duplicates of
frames already in the dataset. `src/edgeforge/curation/` runs blur/exposure/corruption
rejection, perceptual-hash plus embedding-space dedupe, and stratified sampling against
the scenario taxonomy before anything reaches a labeler. Labeling budget is the scarcest
resource in the loop.

**Labeling is teacher-student, human-verified.** A large open-vocabulary detector runs as
an AML managed online endpoint and pre-labels every frame. Humans only adjudicate frames
where teacher confidence is low or teacher and current-production-model disagree. This is
the single biggest cost lever in the system.

**Training is curriculum, not one-shot.** Pre-train on synthetic (Isaac Sim, domain
randomized), fine-tune on real, then distill the large model into the edge-sized student.
The teacher never ships; only the student does.

**Evaluation is closed-loop, not just mAP.** A model that improves mAP but degrades
time-to-brake in the scenario replay suite is a regression. Gates in
`src/edgeforge/evaluation/gates.py` are hard: no gate, no promotion.

**Optimization is measured on real silicon.** INT8 quantization error and latency are
measured on a hardware-in-the-loop rack of the exact target module registered as a
self-hosted runner — never estimated from cloud GPU numbers.

**Rollout is ringed with automatic rollback.** Canary (2 robots) → pilot (1 site) →
production, each ring gated on live fleet health metrics with a twin-driven rollback that
does not require a new deployment.

---

## 3. Repository layout

```
docs/                    Architecture, mapping, security, cost, runbook
infra/                   Terraform for the whole Azure footprint
pipelines/aml/           Azure ML pipeline + component definitions (YAML v2)
src/edgeforge/           Cloud-side Python: curation, labeling, training, eval, optimize
src/edge_modules/        On-robot IoT Edge modules: perception, curator
deploy/iot-edge/         Base + layered deployment manifests
deploy/rollout/          Ring definitions and the rollout/rollback driver
sim/                     Synthetic data generation scenarios and domain randomization
.github/workflows/       CI, training, promotion, edge release, fleet rollout
```

Read the docs in order:

1. [`docs/01-architecture.md`](docs/01-architecture.md) — the whole system, one page
2. [`docs/02-data-plane.md`](docs/02-data-plane.md) — ingest, lake zones, curation
3. [`docs/03-training-plane.md`](docs/03-training-plane.md) — labeling, sim, training, eval
4. [`docs/04-edge-plane.md`](docs/04-edge-plane.md) — optimize, package, deploy, feedback
5. [`docs/05-aws-to-azure-mapping.md`](docs/05-aws-to-azure-mapping.md) — from the AWS reference
6. [`docs/06-security-and-governance.md`](docs/06-security-and-governance.md)
7. [`docs/07-cost-model.md`](docs/07-cost-model.md)
8. [`docs/08-runbook.md`](docs/08-runbook.md)

---

## 4. Quickstart

```bash
# 0. Prereqs: az CLI, terraform >= 1.6, python >= 3.11, ml extension
az login && az account set -s "$AZ_SUBSCRIPTION_ID"
az extension add -n ml

# 1. Stand up the footprint (~25 min; GPU quota must exist in the region)
cd infra
cp terraform.tfvars.example terraform.tfvars   # edit: prefix, region, cidrs
terraform init && terraform apply

# 2. Register environments, components, and the scenario taxonomy
make register-aml

# 3. Curate a raw drop into a versioned dataset
make curate RAW_URI="azureml://datastores/raw/paths/site-alpha/2026-08-01/"

# 4. Train + evaluate (submits an AML pipeline job, streams to MLflow)
make train MODEL=hazard-seg DATASET=mr1-hazard:12

# 5. Optimize, package, and sign an edge bundle for a promoted model
make edge-bundle MODEL=hazard-seg VERSION=41 TARGET=orin-agx-64

# 6. Roll to the canary ring
make rollout BUNDLE=hazard-seg:41 RING=canary
```

Every `make` target is a thin wrapper over an `az ml` / `az iot` call —
see the [`Makefile`](Makefile). Nothing is hidden behind bespoke tooling.

---

## 5. What is deliberately not here

- **No robot-specific autonomy code.** Planning, control, and state estimation live in
  the vehicle repo. `edgeforge` owns the *learned* components only: perception,
  traversability, and anomaly heads.
- **No safety case.** The functional-safety argument (IEC 61508 / ISO 17757 for earth-moving
  machinery) is a separate artifact. This pipeline *produces evidence* for it —
  versioned datasets, model cards, gate results, field telemetry — but does not make it.
- **No production secrets.** Everything authenticates via Entra ID managed identity or
  workload identity federation. There are no keys to leak; see
  [`docs/06-security-and-governance.md`](docs/06-security-and-governance.md).
