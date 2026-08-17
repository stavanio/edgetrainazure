# 01 — Architecture

## 1.1 The workload

| Property | Value |
|---|---|
| Platform | `MR-1`, Class-4 subterranean haulage & inspection robot |
| Fleet scale (design point) | 40 robots across 6 sites, target 400 |
| On-board compute | NVIDIA Jetson AGX Orin 64 GB, 40 W sustained budget |
| Sensors | 4× stereo pair (1920×1200 @ 20 Hz), 1× 32-beam LiDAR @ 10 Hz, IMU @ 200 Hz, wheel odometry, dust/gas |
| Connectivity | None underground; 802.11ax at surface staging, ~50 Mbit/s shared |
| Raw data rate | ~180 GB per robot per 8 h shift (pre-curation) |
| Learned components | `hazard-seg`, `drivable-surface`, `personnel-det`, `equipment-anomaly` |
| Latency budget (perception) | 45 ms p99, sensor-photon to obstacle list |

Two facts drive the entire design:

1. **The uplink is the bottleneck, not the GPU.** 40 robots × 180 GB/shift is 7.2 TB/day
   of raw data against a shared site link. Curation must happen *on the robot* first, and
   again in the cloud before labeling.
2. **The model must fit the robot, not the cloud.** Every architectural decision in the
   training plane exists to produce something that runs in 45 ms at 40 W.

## 1.2 Planes

The system is three planes with one control loop across them.

### Data plane — "what the robot saw"

```
robot ring buffer (MCAP)
   └─ priority score, on-robot ─────┐
                                    ▼
      site staging (IoT Edge blob, store-and-forward)
                                    │
        ┌───────────────────────────┼────────────────────────┐
        ▼                           ▼                        ▼
  IoT Hub (telemetry,        ADLS Gen2 /raw          Azure Data Box
  twins, control)            (immutable, WORM)       (bulk backfill)
        │                           │
        └──────────┬────────────────┘
                   ▼
        Databricks / Delta Lake
        /raw → /clean → /curated → /labeled → /snapshot
```

Four medallion zones plus an immutable `/snapshot` zone. A training run never reads
`/curated` directly — it reads a **frozen, content-addressed snapshot** so that any model
can be re-derived byte-for-byte two years later. This is the lineage requirement for the
safety case.

### Training plane — "what the robot should have concluded"

```
/curated ──▶ AML Data Labeling ──▶ /labeled ──┐
    │            ▲                            │
    │            │ adjudication queue          ├──▶ AML Pipeline
    │      teacher endpoint (open-vocab)      │      ├─ pretrain (synthetic)
    │            ▲                            │      ├─ finetune (real)
    │            │                            │      ├─ distill (→ student)
sim/ ──▶ Isaac Sim on Azure Batch ────────────┘      ├─ evaluate (golden + closed-loop)
         (domain randomized)                          └─ gate → AML Registry
```

### Edge plane — "what the robot will run"

```
AML Registry (model, version, lineage)
   ▼
optimize job on HIL rack (Orin, self-hosted runner)
   ├─ ONNX export → shape/opset validation
   ├─ INT8 PTQ with calibration set from /snapshot
   ├─ TensorRT engine build for exact SM + JetPack
   └─ measured: latency p99, power, accuracy delta
   ▼
edge bundle (OCI artifact in ACR)
   engine + preprocessing config + model card + SBOM + cosign/notation signature
   ▼
IoT Edge layered deployment, targeted by device-twin tag
   canary(2) → pilot(1 site) → production(all)
   ▼
robot: perception module loads engine, curator module scores frames ──▶ back to data plane
```

## 1.3 Azure footprint

| Layer | Resource | Notes |
|---|---|---|
| Network | 1 hub VNet + spoke per environment, no public egress from ML subnet | Private Endpoints for every PaaS service |
| Identity | Entra ID, user-assigned managed identities per workload | No connection strings anywhere |
| Ingest | IoT Hub (S2, ×2 units) + DPS with TPM attestation | Device identity is hardware-rooted |
| Lake | ADLS Gen2, hierarchical namespace, lifecycle → Cool → Archive | `/raw` immutable-blob policy, 7-year hold |
| Compute (ETL) | Azure Databricks premium, Unity Catalog | Delta tables, photon for curation scans |
| Compute (train) | AML compute clusters: `Standard_NC96ads_A100_v4` (train), `Standard_NC24ads_A100_v4` (eval) | Low-priority nodes for sweeps |
| Compute (sim) | Azure Batch, `Standard_NV36ads_A10_v5` pool, spot | Isaac Sim headless containers |
| Compute (HIL) | On-prem Orin rack via Arc-enabled servers + self-hosted GH runners | Only place TensorRT engines are built |
| Registry | ACR Premium, geo-replicated, arm64 + amd64 | Models stored as OCI artifacts alongside images |
| Secrets | Key Vault Premium (HSM) for signing keys | Notation certificate lives here |
| Observability | Log Analytics + App Insights + Azure Managed Grafana | One workspace, fleet + pipeline dashboards |
| Governance | Microsoft Purview, AML Registry lineage | Dataset → model → deployment → robot, queryable |

Full definitions in [`infra/`](../infra).

## 1.4 Environments

Three: `dev`, `stage`, `prod`. They are **separate subscriptions**, not separate resource
groups, so that a runaway sweep in `dev` cannot consume `prod` GPU quota. Promotion between
them is an artifact copy (AML Registry cross-workspace share), never a re-train.

| | dev | stage | prod |
|---|---|---|---|
| Data | 5% sample, faces/plates blurred | full, blurred | full |
| Fleet targets | HIL rack only | 1 test robot | canary → all |
| GPU quota | 8× A100 | 8× A100 | 64× A100 |
| Gates enforced | advisory | blocking | blocking + 2-person approval |

## 1.5 The control loop

The thing that makes this a *system* and not a pile of jobs:

1. `curator` on each robot scores every frame for **novelty** (embedding distance to the
   deployed dataset centroid) and **uncertainty** (predictive entropy + an OOD score from
   the deployed model's penultimate features).
2. Frames above threshold are queued for priority upload. The threshold is a **device-twin
   desired property** — the cloud can retune what the fleet finds interesting without a
   software deployment.
3. Uploaded frames land in `/raw`, get curated, and are routed to the labeling queue with
   a `reason` tag (`novel`, `uncertain`, `disagreement`, `safety-event`, `disengagement`).
4. Fleet drift monitors compare live prediction distributions to the evaluation snapshot.
   Sustained drift on any slice opens a retraining work item automatically.
5. Retrain, gate, package, ring out. Back to 1.

Median loop time, design target: **11 days** from a novel field condition being observed
to a hardened model running on the full fleet. See
[`docs/08-runbook.md`](08-runbook.md) for where that time actually goes.
