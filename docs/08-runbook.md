# 08 — Runbook

## 8.1 The 11-day loop, where the time goes

Design target from "a novel field condition is observed" to "hardened model on the full
fleet":

| Day | Phase | Owner | Automated? |
|---|---|---|---|
| 0 | Field condition observed; curator scores it novel; T1 upload | fleet | ✅ |
| 0–1 | Ingest, quality gates, dedupe, stratify | platform | ✅ |
| 1 | Teacher auto-label; triage routing | platform | ✅ |
| 1–4 | **Human adjudication of routed frames** | annotation | ❌ ← longest pole |
| 4 | Snapshot cut, dataset version registered | platform | ✅ |
| 4–6 | Pretrain / finetune / distill | ML | ✅ |
| 6 | Evaluation, slice gates, closed-loop replay | ML | ✅ |
| 6–7 | **Human promotion review** `Evaluated → Approved` | ML + safety | ❌ |
| 7 | Optimize on HIL, package, sign | platform | ✅ |
| 7–8 | HIL ring soak | platform | ✅ |
| 8–9 | Canary ring, 1 supervised shift | ops | ⚠️ supervised |
| 9–11 | Pilot ring, 3 shifts | ops | ⚠️ supervised |
| 11 | Production rollout | ops | ⚠️ 2-person approval |

Nine of eleven days are automated. The two human steps — adjudication and promotion review
— are deliberately human and should stay that way. If the loop is running long, the fix is
almost always adjudication queue depth, not compute.

## 8.2 Common operations

### Cut a dataset snapshot

```bash
make snapshot DATASET=mr1-hazard NOTE="add site-delta wet-decline frames"
# → registers azureml:mr1-hazard:<n> with Merkle root tag, prints coverage report
```

Check the coverage report before training. If a taxonomy cell fell below its floor, the
sampler will say so — training on it anyway produces a model that fails that slice gate at
day 6 and wastes the cycle.

### Train

```bash
make train MODEL=hazard-seg DATASET=mr1-hazard:12
make train MODEL=hazard-seg DATASET=mr1-hazard:12 SWEEP=1   # HPO, low-priority nodes
```

Follow in MLflow. A run that has not improved validation loss by step 4,000 is almost always
a data problem, not a hyperparameter one — kill it and look at the snapshot.

### Inspect why a gate failed

```bash
make gate-report RUN=<aml_run_id>
```

Prints the tier-1/2/3 table with per-slice deltas against the incumbent, and dumps the worst
50 failing frames to a browsable HTML report. Start with the failing slice, not the aggregate.

### Build and sign an edge bundle

```bash
make edge-bundle MODEL=hazard-seg VERSION=41 TARGET=orin-agx-64
```

Runs on the HIL rack. Takes ~35 min, mostly TensorRT INT8 calibration. Fails loudly if
p99 > 45 ms, power out of envelope, or accuracy delta > 0.8 mAP.

### Roll out

```bash
make rollout BUNDLE=hazard-seg:41 RING=canary
make rollout BUNDLE=hazard-seg:41 RING=pilot
make rollout BUNDLE=hazard-seg:41 RING=production   # requires 2 approvals
```

Each call verifies the signature, patches twins for that ring, and starts the health watcher.

### Roll back

```bash
make rollback RING=canary                    # to last-known-good
make rollback RING=production BUNDLE=hazard-seg:40   # to a specific version
```

Automatic rollback fires on the predicates in [`docs/04-edge-plane.md`](04-edge-plane.md).
The manual command exists for the cases a metric does not catch — an operator saying "it
feels wrong" is a valid reason and does not need justification at the time.

### Retune fleet curiosity without deploying

```bash
make twin-patch RING=production \
  KEY=curator.thresholds.novelty VALUE=0.42
```

Use when a new condition appears and the fleet is not collecting enough of it. Effective
next shift. Lower the threshold, watch the upload queue depth, raise it back when the cell
is full.

## 8.3 Incident playbooks

### P1 — Missed personnel detection reported in the field

1. **Stop the rollout.** `make rollout-freeze` halts all ring advancement fleet-wide.
2. **Do not roll back yet.** Determine whether the incumbent is also affected. If it is, the
   problem is the dataset, not the release, and rolling back makes it worse.
3. Pull the T0 event window (`±30 s`, all sensors) from `/raw` by device and timestamp.
4. Replay the exact frames against: deployed bundle, incumbent bundle, teacher.
5. If the deployed bundle is uniquely wrong → `make rollback RING=production`, then root-cause.
6. Regardless of outcome, the frames are **added to the golden set permanently**, and a slice
   is created for the condition if one does not exist.
7. Post-incident: the gate that should have caught this either did not exist or was not
   sensitive enough. Fix the gate before fixing the model.

### P2 — Fleet-wide OOD spike

Usually physical, not model: new equipment, changed lighting, a re-muck, a new drift.

1. Check whether the spike is one site or fleet-wide. One site → environment change.
   Fleet-wide → suspect the release.
2. Sample 200 high-OOD frames and look at them. This takes ten minutes and resolves it most
   of the time.
3. If environmental: lower the novelty threshold for that site, let the fleet collect, and
   schedule a retrain. No rollback needed.
4. If release-related: rollback, then investigate calibration/preprocessing skew first —
   it is the most common cause.

### P3 — Latency regression after deployment

1. Check `throttled_pct` before anything else. A thermal problem masquerading as a model
   problem is common, especially at ambient extremes.
2. Compare HIL-measured p99 against field p99. A gap means the concurrent-load profile on
   the HIL rack no longer matches the robot — fix the HIL load generator, because every
   future measurement is wrong until you do.
3. If genuinely the model: rollback, then re-examine DLA placement and the FP16 fallback
   layer list.

### P4 — Ingest backlog

1. Check queue depth per site in Grafana. A single site → site link problem.
2. Raise the curator threshold temporarily to shed T2 traffic; T0 is never shed.
3. If sustained > 72 h, order a Data Box for that site. Do not let the ring buffer overwrite
   T1 frames that were scored interesting — that is silently losing the most valuable data in
   the system.

## 8.4 Standing maintenance

| Cadence | Task | Why |
|---|---|---|
| Weekly | Reproducibility canary — re-run a fixed job, compare metrics | Catches environment drift before it invalidates a release |
| Weekly | Annotator κ review on calibration set | Label quality degrades silently |
| Monthly | Golden-set growth review | It must grow with the ODD, or the gates go stale |
| Monthly | Rejection-rate trend per camera per robot | Earliest hardware-degradation signal available |
| Quarterly | **Rollback rehearsal on a live ring** | An untested rollback is not a rollback |
| Quarterly | Cost review against `docs/07-cost-model.md` | Drift is normal; surprise is not |
| Quarterly | Trust-store and certificate rotation review | |
| Annually | Full lineage audit — pick a deployed model, reconstruct it from `/snapshot` | This is the claim the safety case rests on |

## 8.5 Things that will bite you

Collected failure modes, offered without dressing:

- **Preprocessing skew.** Different resize interpolation between training and the edge
  config. Symptom: eval great, field mediocre, no obvious cause. Prevention: preprocessing
  config is a shipped artifact, and the HIL test compares end-to-end outputs, not just engine
  outputs.
- **Calibration set that is not stratified.** INT8 calibration on a random sample
  under-represents rare cells and quantization error concentrates exactly where you cannot
  afford it. Always stratify the calibration set.
- **Golden set contamination.** Someone adds field frames to both train and golden. Prevented
  by IAM here, but check the lineage audit anyway.
- **Twin patch race during rollout.** Two rollouts targeting overlapping rings. The rollout
  driver takes a fleet-wide advisory lock; do not bypass it.
- **Silent taxonomy drift.** New conditions appear in the field that have no taxonomy cell,
  so stratification cannot protect them and slice gates cannot see them. The OOD monitor is
  the backstop; review new-cell proposals monthly.
- **A model that improves by getting better at empty drifts.** Aggregate metrics reward it,
  the fleet distribution rewards it, and it is worse at the job. This is what tier-2 slice
  gates exist for. Never promote on aggregate alone.
