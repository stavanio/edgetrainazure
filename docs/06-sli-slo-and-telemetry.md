# 06 — SLIs, SLOs, telemetry, and dashboards

> **Design artifact.** This describes an architecture that has not been deployed.
> Figures are design targets and planning assumptions, not measurements —
> see the status table in the [README](../README.md).

Everything in this document exists to answer three questions without anyone having
to ask a person:

1. **Is the fleet safe right now?**
2. **Is the loop turning, and where is it stuck?**
3. **Is this getting cheaper per robot, or more expensive?**

The definitions live as code in [`observability/slo.yaml`](../observability/slo.yaml).
The evaluator is [`src/edgeforge/fleet/slo.py`](../src/edgeforge/fleet/slo.py). The
queries behind every panel are in [`observability/queries/`](../observability/queries).
Nothing here is a screenshot of a dashboard someone built by hand.

## 6.1 Invariants vs. objectives

Most SLIs get an objective and an error budget: a target, a window, and an
explicit allowance for failure. That is correct for reliability, and wrong for
safety.

Four indicators are **invariants**. They have **no error budget**, no burn-rate
alerting, and no "we're within budget for the quarter" conversation. A single
breach freezes releases fleet-wide and opens an incident.

| Invariant | Breach condition | Consequence |
|---|---|---|
| `missed_personnel` | Any frame with a person undetected inside the safety envelope, in evaluation or in the field | Release blocked; frame joins the golden set permanently |
| `bundle_signature` | Any device running a bundle that fails signature verification | Device quarantined; supply-chain incident |
| `safety_envelope` | Any safety-envelope violation attributed to perception | Immediate ring rollback |
| `golden_set_isolation` | Any training identity granted read access to the golden storage account | Release blocked; all models since the grant re-evaluated |

Treating these as percentages would be a category error. "99.9% of shifts with no
missed personnel" is not an objective anyone should be willing to write down.

Everything else below is a genuine SLO with a genuine error budget.

## 6.2 The SLI catalogue

25 indicators in four groups: 4 safety invariants (§6.1) and 21 service-level
objectives. Each has a precise numerator and denominator — "availability" with an
unstated denominator is not an SLI.

### Group A — Safety (invariants, §6.1)

| ID | Indicator | Measurement |
|---|---|---|
| `A1` | Missed personnel events | Count, fleet + replay suite |
| `A2` | Bundle signature verification | Devices verified / devices running |
| `A3` | Safety envelope violations | Count, attributed to perception |
| `A4` | Golden-set isolation | Role assignments on the golden account |

### Group B — Fleet reliability

| ID | Indicator | Good event / valid event | SLO | Window |
|---|---|---|---|---|
| `B1` | Perception availability | Shift-minutes producing detections / shift-minutes operating | **99.5%** | 28 d |
| `B2` | Inference latency | Frames under 45 ms p99 / frames inferred | **99.0%** | 28 d |
| `B3` | Frame completeness | Frames inferred / frames captured | **99.5%** | 28 d |
| `B4` | Thermal headroom | Sample-minutes not throttled / sample-minutes operating | **99.0%** | 28 d |
| `B5` | Bundle convergence | Devices on intended bundle / devices online, 4 h after rollout | **99.0%** | 28 d |
| `B6` | Rollback latency | Rollbacks completing ≤ 5 min / rollbacks initiated | **99.0%** | 90 d |
| `B7` | Model freshness | Devices within 2 releases of current / devices in fleet | **95.0%** | 28 d |

### Group C — Pipeline health

| ID | Indicator | Good event / valid event | SLO | Window |
|---|---|---|---|---|
| `C1` | T0 event delivery | Safety events in `/raw` within 24 h of surfacing / safety events recorded | **99.9%** | 28 d |
| `C2` | Ingest freshness | T1 shards curated within 36 h / T1 shards uploaded | **95.0%** | 28 d |
| `C3` | Curation success | Curation jobs completing unattended / curation jobs started | **98.0%** | 28 d |
| `C4` | Label queue latency | Frames adjudicated within 72 h / frames routed to humans | **90.0%** | 28 d |
| `C5` | Training reproducibility | Weekly canary re-runs within tolerance / canary re-runs | **100%** | 90 d |
| `C6` | Gate evidence completeness | Releases with full slice + closed-loop evidence / releases | **100%** | 90 d |
| `C7` | Loop time | Loops completing ≤ 11 days / loops completed | **80.0%** | 90 d |

### Group D — Efficiency and unit economics

These are **efficiency SLIs**: same structure, same error budgets, but the
consequence of a breach is a cost conversation rather than an incident. They are
on the same dashboards as everything else deliberately — separating "is it working"
from "what does it cost" is how platforms quietly become unaffordable.

| ID | Indicator | Definition | Objective | Window |
|---|---|---|---|---|
| `D1` | Label auto-accept rate | Auto-accepted frames / frames labeled | **≥ 70%** | 28 d |
| `D2` | Cost per labeled frame | Labeling spend / frames added to `/labeled` | **≤ $0.32** | 28 d |
| `D3` | Uplink yield | Frames surviving to `/curated` / frames uploaded | **≥ 40%** | 28 d |
| `D4` | GPU utilization | GPU-seconds busy / GPU-seconds allocated | **≥ 65%** | 28 d |
| `D5` | Interruptible-compute share | Spot GPU-hours / interruptible-eligible GPU-hours | **≥ 90%** | 28 d |
| `D6` | Cost per production release | Total attributable spend / releases reaching production | **≤ $34k** | 90 d |
| `D7` | Cost per robot-month | Total platform spend / active robots | **trending ↓** | 90 d |

`D7` is the only indicator whose objective is a *direction* rather than a
threshold, because the absolute number depends on fleet size. It is evaluated as a
regression slope over the trailing 90 days, and it is the number to put in front
of a board.

## 6.3 Error budgets and burn rate

For an SLO with target `T` over window `W`, the error budget is `1 − T` of the
valid events in `W`. Burn rate is the observed error rate divided by the rate that
would exactly exhaust the budget over `W`.

```
burn_rate = (1 − SLI_observed) / (1 − T)
```

A burn rate of 1 exhausts the budget exactly at the end of the window. A burn rate
of 14.4 exhausts it in 2% of the window.

### Multi-window, multi-burn-rate alerting

Single-threshold alerting on an SLI is either too noisy or too slow. `edgeforge`
uses the standard two-tier scheme, with both a long and a short window required
to fire — the short window is what stops an alert from persisting long after the
problem has resolved.

| Severity | Burn rate | Long window | Short window | Budget consumed | Action |
|---|---|---|---|---|---|
| **Page** | 14.4× | 1 h | 5 m | 2% | Wake someone |
| **Page** | 6× | 6 h | 30 m | 5% | Wake someone |
| **Ticket** | 3× | 24 h | 2 h | 10% | Work item, next business day |
| **Ticket** | 1× | 72 h | 6 h | 10% | Work item, next business day |

Implemented in `slo.py::evaluate_burn_rate`; provisioned as Azure Monitor
scheduled query rules in [`infra/observability.tf`](../infra/observability.tf).

### What the budget is *for*

An error budget that is never spent means the objective is too loose or the team
is too cautious. The budget is permission to take risk:

- **Budget healthy (> 50% remaining):** ship. Advance rings on the normal cadence.
- **Budget low (10–50%):** ship, but no discretionary changes to the edge plane.
- **Budget exhausted (< 10%):** feature freeze on the affected plane until the
  budget recovers. Reliability work takes priority over model improvements.

This is a policy, not a suggestion, and it is encoded in
`slo.py::release_posture`.

## 6.4 The telemetry data model

Three streams. Each has a fixed, versioned contract — adding a field is additive
and safe, changing a field's meaning is a version bump.

### Stream 1 — fleet health (`kind: health`, every 30 s, per robot)

Routed IoT Hub → Event Hubs → Log Analytics as `FleetHealth_CL`. This is the
stream every fleet SLI and every rollback predicate reads.

```jsonc
{
  "kind": "health", "schema_version": 2,
  "device_id": "mr1-014", "site": "alpha", "ring": "canary",
  "bundle": "hazard-seg:41-orin-agx-64-jp6.0", "release_id": "8821-1",
  "ts": 1755400000,

  // B2, B3 — latency and completeness
  "inference_p50_ms": 21.4, "inference_p99_ms": 38.9,
  "frames_captured": 1200, "frames_inferred": 1198, "dropped_frames_pct": 0.02,

  // B1, B4 — availability and thermal headroom
  "operating_seconds": 30, "detecting_seconds": 30,
  "power_w_avg": 33.1, "soc_temp_c": 71.2, "throttled_pct": 0.0,

  // Drift and rollback predicates
  "detections_per_km": {"personnel": 0.31, "machine": 2.04, "hazard": 1.77},
  "mean_confidence": 0.83, "ood_rate": 0.011,

  // A1, A3 — invariants
  "safety_envelope_violations": 0, "disengagements": 0,

  // D3 — uplink yield numerator source
  "queue_depth": 42, "uploaded_frames": 310, "retained_frames": 128
}
```

**Cardinality discipline.** `detections_per_km` is a fixed three-key map, not a
per-class dimension. Telemetry cost scales with cardinality × fleet size, and an
unbounded label is how an observability bill quietly overtakes a training bill.
High-cardinality detail belongs in the drift sketch, sampled.

### Stream 2 — drift sketch (`kind: drift_sketch`, every 30 s, per robot)

Routed IoT Hub → ADLS as Avro, batched every 5 minutes. Distribution *shape*, not
raw predictions — it must be small enough to send continuously from the whole
fleet. Consumed by the drift monitors and the `D3` uplink-yield calculation.

### Stream 3 — pipeline events (`kind: pipeline_event`, per stage transition)

Emitted by every AML job, curation run, gate evaluation, build, and rollout.
One event per stage transition, correlated by `release_id` and `loop_id`. This is
what makes `C7` (loop time) measurable end to end rather than a number someone
estimates in a retro.

```jsonc
{
  "kind": "pipeline_event", "schema_version": 1,
  "loop_id": "loop-2026-08-04-alpha", "release_id": "8821-1",
  "stage": "gate_evaluation", "status": "passed",
  "started_utc": "2026-08-10T04:12:00Z", "ended_utc": "2026-08-10T05:41:00Z",
  "attributable_cost_usd": 1180.44,
  "actor": "automated",
  "detail": {"gates_failed": 0, "slices_evaluated": 312}
}
```

`attributable_cost_usd` on every event is what makes `D6` (cost per release) a
measurement rather than a monthly reconciliation exercise.

## 6.5 Dashboards

Three, provisioned as code in [`observability/dashboards/`](../observability/dashboards)
and deployed to Azure Managed Grafana by Terraform. Each answers exactly one of
the three questions at the top of this document.

### `fleet-health` — is the fleet safe right now?

Audience: on-call, ops supervisors. Refresh 1 m.

Top row is four **stat tiles**, not charts, because a number that is either fine
or not fine does not need a plot: invariant breaches (24 h), perception
availability, devices on intended bundle, active rollbacks. Below that:
latency p99 by ring over time, personnel detections per km against the ring
baseline band, thermal headroom by site, and an OOD-rate small-multiple per site.

Series are colored by **ring** — the same ring keeps the same color no matter how
the filter changes, so canary is never repainted when pilot drops out of view.
Threshold and status marks use the reserved status palette and always carry a
label, never color alone.

### `pipeline-health` — is the loop turning?

Audience: platform and ML teams. Refresh 5 m.

A funnel from frames captured → uploaded → quality-passed → deduped → curated →
routed → labeled → in snapshot, with the stage-over-stage retention shown as the
bar's own annotation. Then loop-time distribution against the 11-day objective,
label-queue depth and age, curation job success, and the reproducibility canary.

The funnel is the panel that answers "where is it stuck" at a glance, which is the
question the runbook's P4 playbook starts from.

### `efficiency` — what does it cost, and which way is it going?

Audience: engineering leadership, finance. Refresh 1 h.

Hero number is **cost per robot-month** with its 90-day trend — the number that
goes in a board pack. Around it: cost per labeled frame, cost per production
release, auto-accept rate against its 70% objective, GPU utilization, spot share,
and a spend breakdown by stage over time.

Cost panels are indexed to a common base rather than plotted on a second axis. A
dual-axis chart comparing dollars to percentages is the fastest way to make a cost
trend say whatever the reader already believed.

## 6.6 Reviewing this

| Cadence | Review | Owner |
|---|---|---|
| Per shift | Fleet health dashboard, invariant breaches | Ops supervisor |
| Weekly | Error budget consumption by group; release posture | Platform lead |
| Monthly | Efficiency SLIs against objectives; `D7` slope | Engineering leadership |
| Quarterly | **Are these the right SLIs?** Retire ones nobody reads; add ones an incident showed we were missing | Platform + safety |

The quarterly review is the one that gets skipped and shouldn't. An SLI catalogue
that never changes is a catalogue that has stopped describing the system.
