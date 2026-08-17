# 02 — Data plane

## 2.1 On-robot capture and triage

The robot never uploads everything. It cannot — 180 GB/shift against a shared surface link
that the whole site contends for.

The `curator` module ([`src/edge_modules/curator/`](../src/edge_modules/curator)) maintains a
**60-minute rolling MCAP ring buffer** on NVMe and applies a three-tier retention policy:

| Tier | Trigger | Retention | Upload priority |
|---|---|---|---|
| **T0 — event** | Safety stop, operator disengagement, planner fault, e-stop | Permanent, ±30 s window | Immediate, blocking |
| **T1 — interesting** | Novelty or uncertainty above twin threshold | 7 days | High, on next link |
| **T2 — background** | Deterministic sample, 1 frame / 4 s / camera | 24 h | Low, best effort |

Everything else is dropped at the ring buffer. Typical retained volume: **~2.1 GB/shift/robot**,
an 85× reduction, and the retained fraction is *far* more informative than a uniform sample.

Scoring runs on the same Orin that runs perception, in the gap between inference frames:

```
priority = w_e · H(p)              # predictive entropy of the deployed head
         + w_o · ood(z)            # Mahalanobis distance in penultimate feature space
         + w_n · (1 − cos(z, C))   # novelty vs. deployed-dataset centroid C
         + w_d · 1[disagree]       # deployed model vs. cheap geometric prior
         + w_s · 1[safety_event]
```

Weights `w_*`, the thresholds, and the centroid `C` are all **device-twin desired
properties**. Retuning fleet-wide curiosity is a twin patch, not a deployment — this
matters enormously when a new failure mode appears and you need the fleet hunting for it
by end of shift.

## 2.2 Transport

Three paths, chosen by size and urgency:

**Telemetry → IoT Hub.** Structured, small, continuous-ish: health, model version in use,
inference latency histograms, prediction-distribution sketches, priority-queue depth.
Routed by IoT Hub message routing to Event Hubs → Stream Analytics → Log Analytics for
dashboards, and to ADLS for the drift monitors.

**Payload → ADLS Gen2 direct.** MCAP shards do *not* go through IoT Hub (wrong tool, wrong
cost). The `curator` requests a short-lived, path-scoped **user-delegation SAS** from a
Function App that authorizes on device identity, then does a block-blob upload with
resumable chunking. The device holds no storage credential, ever.

**Backfill → Azure Data Box.** When a site's retained volume outruns its uplink — common
during commissioning, when everything is novel — a Data Box ships. The landing path and
manifest format are identical, so nothing downstream knows the difference.

Upload is idempotent and content-addressed: `sha256` of the shard is the blob name suffix,
so a retried or duplicated upload is a no-op.

### Raw layout

```
abfss://raw@<account>.dfs.core.windows.net/
  site=<site_id>/
    robot=<device_id>/
      date=<YYYY-MM-DD>/
        shift=<A|B|C>/
          tier=<t0|t1|t2>/
            <epoch_ms>-<sha256[:16]>.mcap
            <epoch_ms>-<sha256[:16]>.manifest.json
```

Partitioned for the queries that actually get run: "everything from site alpha last week",
"all T0 events fleet-wide", "this robot's last 30 days". `/raw` has an **immutability
policy** (time-based retention, 7 years, legal-hold capable) — it is evidence.

## 2.3 Medallion zones

| Zone | Format | Written by | Guarantee |
|---|---|---|---|
| `/raw` | MCAP + manifest | fleet | Immutable, never rewritten |
| `/clean` | Delta (frame-level) | `curation.quality_gates` | Decoded, calibrated, quality-passed |
| `/curated` | Delta | `curation.dedupe_and_sample` | Deduped, stratified, PII-redacted |
| `/labeled` | Delta + COCO/mask sidecars | labeling + `labeling.merge` | Has ground truth, has provenance |
| `/snapshot` | Delta deep clone, read-only | `curation.snapshot` | Frozen, content-addressed, immutable |

### `/clean` — quality gates

[`src/edgeforge/curation/quality_gates.py`](../src/edgeforge/curation/quality_gates.py).
Rejection is logged with a reason, never silent — the rejection-rate time series is itself
a fleet health signal (a camera going soft shows up here weeks before anyone notices).

| Gate | Metric | Reject when | Typical rate |
|---|---|---|---|
| Blur | variance of Laplacian | `< 55` | 6% |
| Exposure | fraction of pixels clipped | `> 12%` at either end | 4% |
| Dust occlusion | dark-channel prior haze estimate | `> 0.62` | 9% |
| Sync | max inter-sensor timestamp skew | `> 8 ms` | 1% |
| Calibration staleness | days since last intrinsics check | `> 30` | flag only |
| Integrity | CRC / decoder error | any | 0.3% |

Dust occlusion is the domain-specific one and it matters: a naive pipeline trains happily
on frames a human could not interpret, and the model learns to be confident in a whiteout.

### `/curated` — dedupe and stratify

[`src/edgeforge/curation/dedupe_and_sample.py`](../src/edgeforge/curation/dedupe_and_sample.py).

1. **Near-duplicate removal, two stage.** Perceptual hash (fast, catches a stationary robot
   emitting identical frames) then embedding cosine similarity within a temporal and spatial
   neighbourhood (catches the same drift traversed twice at different speeds). Typically
   removes 60–70% of what survives quality gates.
2. **Stratification against the scenario taxonomy.** Every frame is tagged with
   `(illumination, dust_level, surface_class, geometry_class, personnel_present, machine_present)`.
   The sampler enforces per-cell floors so that rare-but-critical cells — *personnel present,
   high dust, low light* — are never sampled out just because they are rare. This is where
   most naive pipelines quietly fail: uniform sampling produces a dataset whose distribution
   matches the fleet's, and the fleet mostly drives down empty, well-lit main drifts.
3. **PII redaction.** Face and high-vis-vest-number blurring, applied before any human sees
   a frame, non-reversibly in `/curated`. The unblurred original stays in `/raw` under
   access control. Required for works-council and GDPR sign-off at EU sites.

### `/snapshot` — the thing you actually train on

A training run never reads a live table. `curation.snapshot` performs a Delta **deep clone**
into `/snapshot/<dataset>/<version>/`, computes a Merkle root over the file list, registers
it as an AML data asset with that root as a tag, and marks it read-only.

Consequence: `hazard-seg:41` can name exactly the bytes it was trained on, forever. When a
field incident is investigated 18 months later, "what did the model know" is a query, not an
archaeology project.

## 2.4 Retention and cost control

| Zone | Hot | Cool | Archive | Delete |
|---|---|---|---|---|
| `/raw` T0 | 90 d | 1 y | 7 y | never |
| `/raw` T1 | 30 d | 180 d | 2 y | 2 y |
| `/raw` T2 | 7 d | 30 d | — | 30 d |
| `/clean` | 30 d | — | — | 90 d (rederivable) |
| `/curated` | live | — | — | never |
| `/snapshot` | live | 1 y | 5 y | never |

`/clean` is disposable by design: it is a pure function of `/raw` plus pinned code, so it
is cheaper to recompute than to store. `/curated` and `/snapshot` are not — they contain
human labeling effort and irreproducible sampling decisions.

See [`docs/07-cost-model.md`](07-cost-model.md) for what this actually costs.
