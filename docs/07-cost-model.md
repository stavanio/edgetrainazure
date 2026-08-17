# 07 — Cost model

> **Design artifact.** This describes an architecture that has not been deployed.
> Figures are design targets and planning assumptions, not measurements —
> see the status table in the [README](../README.md).

A **bottom-up cost model**, not a bill. Nothing here has been spent. Figures are built
from published Azure list pricing (US East / West Europe, mid-2026) applied to a sized
workload: **40 robots, 6 sites, 4 model families, ~2 production releases per month.**

The purpose is not to predict the invoice to the dollar. It is to find which line dominates,
establish whether the unit economics work at scale, and identify which assumptions the answer
is sensitive to — §7.6. Treat every figure as ±30% at best, and the labeling line as the one
worth arguing about.

## 7.1 Modeled monthly run cost

| Line | Configuration | $/month | Notes |
|---|---|---|---|
| **Storage** | | **~$6,900** | |
| `/raw` hot | 65 TB | $1,300 | T0/T1 recent |
| `/raw` cool | 210 TB | $2,100 | |
| `/raw` archive | 900 TB | $900 | 7-year evidence retention |
| `/clean` | 40 TB hot | $800 | Disposable, short TTL |
| `/curated` + `/labeled` | 55 TB hot | $1,100 | Irreplaceable |
| `/snapshot` | 35 TB mixed | $700 | Frozen training sets |
| **Ingest** | | **~$1,400** | |
| IoT Hub | S2 × 2 units | $500 | Telemetry only, not payload |
| Event Hubs + Stream Analytics | 4 TU + 6 SU | $700 | |
| Egress / SAS broker | Functions consumption | $200 | |
| **Curation** | | **~$4,200** | |
| Databricks | ~600 DBU/mo, photon, spot workers | $4,200 | Bursty; scales with fleet size |
| **Labeling** | | **~$28,000** | ← dominant line |
| Teacher endpoint | 1× A100, ~90 h/mo batch | $2,800 | |
| Human annotation | ~26k adjudicated frames @ ~$0.95 | $25,000 | Post-triage; would be ~$200k naive |
| **Simulation** | | **~$3,100** | |
| Azure Batch A10 spot | ~1,400 GPU-h/mo | $3,100 | Spot; interruptions are harmless here |
| **Training** | | **~$18,500** | |
| Pretrain | 8× A100 × ~40 h × 2/mo | $8,900 | |
| Finetune + distill | 8× A100 × ~18 h × 4/mo | $6,400 | |
| Sweeps | low-priority A100, ~200 GPU-h | $2,100 | |
| Evaluation | 1× A100, ~55 h | $1,100 | |
| **Edge build** | | **~$900** | |
| HIL rack | Amortized hardware + power + Arc | $900 | Hardware capex ~$45k, 3-yr |
| **Registry & delivery** | | **~$700** | |
| ACR Premium + geo-replication (3 regions) | | $500 | |
| Bundle egress to sites | ~1.2 TB/mo | $200 | |
| **Platform** | | **~$2,400** | |
| Log Analytics | ~120 GB/day ingest, commitment tier | $1,600 | |
| Managed Grafana, Key Vault, Purview, Defender | | $800 | |
| | | | |
| **Total** | | **≈ $66,000 / month** | ≈ $1,650 per robot per month |

## 7.2 Where the money goes

```
Labeling      ████████████████████████████████████████████  42%
Training      ██████████████████████████                    28%
Storage       ██████████                                    10%
Curation      ██████                                         6%
Platform      ████                                           4%
Simulation    ████                                           5%
Ingest        ██                                             2%
Other         ██                                             3%
```

**Labeling dominates, and it is the line most sensitive to engineering effort.** On these
assumptions each percentage point of auto-accept rate is roughly $350/month, and the
teacher-student triage in [`docs/03-training-plane.md`](03-training-plane.md) moves labeling
from ~$200k/month naive to ~$25k/month — an 8× reduction that comes entirely from *not asking
humans about frames the system already understands*.

That 8× is the single claim this whole architecture stands or falls on, and it is unverified.
See §7.6.

The second lever is curation. On-robot triage (85× volume reduction) plus cloud dedupe
(another ~3×) means roughly 250× fewer frames reach a labeler than the sensors produce. Without
that, every line in this table is multiplied and the labeling line becomes unpayable.

## 7.3 Scaling

Costs do **not** scale linearly with fleet size, which is the central economic argument for
the flywheel:

| | 40 robots | 120 robots | 400 robots |
|---|---|---|---|
| Storage | $6,900 | $19,000 | $59,000 |
| Ingest | $1,400 | $3,800 | $11,500 |
| Curation | $4,200 | $10,500 | $31,000 |
| **Labeling** | **$28,000** | **$38,000** | **$52,000** |
| Training | $18,500 | $21,000 | $26,000 |
| Simulation | $3,100 | $3,400 | $4,000 |
| Platform + edge + registry | $4,000 | $5,500 | $9,000 |
| **Total** | **$66,000** | **$101,000** | **$192,500** |
| **Per robot** | **$1,650** | **$842** | **$481** |

Storage, ingest, and curation are linear — they track bytes. Labeling, training, and
simulation are **sub-linear**, because a 10× larger fleet does not see 10× more *novel*
things; it sees the same world more times. The curator's novelty scoring is what converts
that redundancy into cost savings instead of storage bills.

Per-robot cost falls ~3.4× from 40 to 400 robots. If the sub-linear assumption holds, that
is the flywheel paying for itself; if novelty does *not* saturate with fleet size, labeling
stays linear and the curve flattens to roughly 1.6×. Which of those is true is an empirical
question about the operating environment, not about the architecture.

## 7.4 Optimization levers, ranked by payback

1. **Raise auto-accept rate.** Better teacher, better agreement heuristics, active-learning
   query strategy. Highest leverage in the system by a wide margin.
2. **Tune on-robot thresholds aggressively.** They are twin properties — measure the marginal
   value of retained frames per site and tighten where a site has gone stale. Costs nothing
   to change.
3. **Reserved instances / savings plans on training GPU.** ~35% on the predictable baseline.
   Keep sweeps on low-priority.
4. **Spot everywhere it is safe.** Simulation and sweeps are fully interruption-tolerant.
   Never spot the final training run — a preempted 40-hour job costs more than it saves.
5. **Lifecycle policy discipline.** `/clean` deletion at 90 days and `/raw` T2 at 30 days are
   worth ~$1,100/month and are pure policy.
6. **Log Analytics commitment tier + sampling.** Telemetry ingest grows with the fleet and is
   easy to over-collect. Sample the high-cardinality histograms; keep the health contract whole.
7. **ACR geo-replication only where sites are.** Each replica is a fixed monthly cost.

## 7.5 What this replaces

For context, not accounting: the alternative to this pipeline is not "no cost." It is manual
data handling, ad-hoc labeling, un-versioned models, no gates, and no rollback path. The
expensive failure mode there is not the cloud bill — it is a model regression discovered by a
robot rather than by a gate.

## 7.6 Load-bearing assumptions

The model is only as good as five numbers, none of which have been measured. Listed in
order of how much damage being wrong would do.

| # | Assumption | Value used | If it is wrong | How to establish it |
|---|---|---|---|---|
| 1 | **Auto-accept rate** — share of frames the teacher and deployed model agree on confidently | 71% | At 40%, labeling roughly doubles to ~$52k/mo and total cost rises ~36%. The architecture still works; the business case weakens sharply. | Run the teacher over a few thousand real frames against a real incumbent. Cheapest possible experiment, highest information. |
| 2 | **On-robot triage retention** — fraction of captured frames worth keeping | ~1.2% (85× reduction) | Every storage, ingest, and curation line scales directly. At 5% retention, storage and curation roughly quadruple. | Instrument the curator in shadow mode on one robot for a week; no cloud needed. |
| 3 | **Cloud dedupe rate** — near-duplicates among quality-passed frames | 60–70% | Feeds straight into labeling volume, compounding with #1. | Offline, on any existing captured dataset. |
| 4 | **Novelty saturation with fleet size** — a 10× fleet does not see 10× novel situations | Strongly sub-linear | This is the entire per-robot cost curve. If wrong, unit economics stay flat instead of improving 3.4×. | Cannot be established before a multi-site fleet exists. The largest standing risk in the model. |
| 5 | **Annotation unit cost** | ~$0.95/frame adjudicated | Linear on the dominant line. Varies widely by vendor, geography, and task complexity. | Vendor quotes against a representative task spec. |

Assumptions 1–3 are all testable **before any Azure resource is created**, on recorded data
and a laptop-scale experiment. That ordering is deliberate: the cheapest experiments retire
the largest uncertainties, and none of them require the platform in this repository to exist.

Assumption 4 is the one that cannot be retired early. It should be stated as a known risk in
any plan built on this model rather than buried in a spreadsheet.
