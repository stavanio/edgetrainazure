# 04 — Edge plane

## 4.1 Optimize

A model that passes every cloud gate is still unproven. The thing that ships is not the
PyTorch checkpoint — it is a TensorRT engine, quantized, built for one exact silicon and
JetPack combination. That transformation changes accuracy, and the change must be measured,
not assumed.

### Pipeline

```
checkpoint (AML Registry, Approved)
  │
  ├─ 1. ONNX export           opset pinned, dynamic axes only where needed
  │                           parity check: max |Δ| vs. PyTorch < 1e-4 over 512 frames
  │
  ├─ 2. INT8 calibration      entropy calibrator over 1,024 frames drawn from /snapshot,
  │                           stratified across taxonomy cells — NOT a random sample
  │                           sensitive layers (final heads, first conv) kept FP16
  │
  ├─ 3. TensorRT build        on the target device, for target SM + JetPack + TRT version
  │                           engines are NOT portable; a build per target SKU
  │
  └─ 4. Measure on silicon    latency p50/p99, power draw, thermal steady state,
                              accuracy delta vs. FP32 on the golden set
```

### Why builds happen on a HIL rack

TensorRT engines are tied to the compute capability, TensorRT version, and often the exact
driver of the machine that built them. Cross-compiling from a cloud A100 produces an engine
that either refuses to load on an Orin or silently performs differently.

So `edgeforge` keeps a **hardware-in-the-loop rack**: real Orin AGX 64 GB modules on the
production carrier board, on the production thermal solution, Arc-enabled and registered as
self-hosted GitHub Actions runners. Every engine is built and measured there.

The rack also measures what a cloud GPU cannot:

| Measured on HIL | Why it cannot be inferred |
|---|---|
| p99 latency under concurrent load | Real robot runs perception, SLAM, planning, logging on one SoC |
| Sustained power at 40 W cap | Orin throttles; a model that fits at t=0 may not at t=20 min |
| Thermal steady state at 45 °C ambient | Mine ambient is not a lab; clocks drop |
| DLA vs. GPU placement | Offloading heads to DLA frees GPU for other consumers |
| Cold-start / engine load time | Matters for the OTA swap window |

Gate: **p99 ≤ 45 ms under representative concurrent load, sustained power within envelope,
accuracy delta ≤ 0.8 mAP versus FP32.** Fail any and the bundle is not produced.

Implementation: [`src/edgeforge/optimize/`](../src/edgeforge/optimize).

## 4.2 Package

The deliverable is an **edge bundle** — a single OCI artifact in ACR containing everything
needed to reproduce the robot's inference behaviour:

```
hazard-seg:41-orin-agx-64-jp6.0
├── engine/hazard-seg.plan            TensorRT engine, INT8/FP16 mixed
├── engine/hazard-seg.onnx            source ONNX (for re-build on JetPack bump)
├── config/preprocess.json            resize, normalization, colour space, letterbox
├── config/postprocess.json           thresholds, NMS, class map, safety-envelope geometry
├── config/runtime.json               batch, streams, DLA assignment, warmup frames
├── calib/calibration.cache           TensorRT calibration cache
├── model_card.md                     metrics, slices, closed-loop, lineage, known limits
├── sbom.spdx.json                    full dependency inventory
└── provenance.intoto.jsonl           SLSA provenance: who built it, from what, where
```

Signed with **Notation** using a certificate in Key Vault Premium (HSM-backed, non-exportable).
The robot verifies the signature before load and **refuses to run an unsigned or
untrusted-chain bundle**. This is the single most important control in the system: it is
what prevents a compromised pipeline, registry, or network position from putting arbitrary
inference behaviour onto a 30-tonne machine.

Preprocessing config ships *with* the engine deliberately. Train/serve preprocessing skew —
a different resize interpolation, a swapped colour channel, a normalization constant that
drifted — is the most common cause of "the model was fine in eval and is bad in the field,"
and it is entirely preventable by making the config an artifact rather than code on both
sides.

## 4.3 Deploy

### IoT Edge, layered

Two layers:

**Base deployment** ([`deploy/iot-edge/deployment.base.json`](../deploy/iot-edge/deployment.base.json)) —
the runtime that rarely changes: `edgeAgent`, `edgeHub`, `curator`, `telemetry`,
`ros2-bridge`, local blob store. Targeted at all robots.

**Layered model deployment**
([`deploy/iot-edge/deployment.layer.model.json`](../deploy/iot-edge/deployment.layer.model.json)) —
only the `perception` module and its bundle reference. Higher priority, targeted by twin tag.
This is what a model release actually changes.

Consequence: shipping a model does not restart the robot's runtime. `perception` reloads;
`curator` keeps buffering; the ROS 2 bridge stays up.

### Rings

[`deploy/rollout/rings.yaml`](../deploy/rollout/rings.yaml)

| Ring | Population | Soak | Advance criteria |
|---|---|---|---|
| `hil` | HIL rack | 2 h | Automated regression suite green |
| `canary` | 2 robots, 1 site, supervised shift | 1 shift (8 h) | All health SLOs green, zero safety events attributable |
| `pilot` | All robots at 1 site | 3 shifts | Above + operator sign-off + no false-stop increase |
| `production` | Fleet | — | Above + 2-person approval |

Targeting is a device-twin tag query — `tags.ring = 'canary'` — so moving a robot between
rings is a twin patch. No deployment edit, no manifest churn.

### Shadow mode

For the first ring, `perception` can run the new bundle **alongside** the incumbent, with
only the incumbent's output wired to the vehicle. The new model's disagreements are logged
and uploaded as T1 priority. Costs latency headroom, so it is used for one shift, not
indefinitely — but it is how you find out that a model behaves differently in a real drift
without letting it drive anything.

## 4.4 Health and rollback

The `perception` module emits a fixed health contract to IoT Hub every 30 s:

```jsonc
{
  "bundle": "hazard-seg:41-orin-agx-64-jp6.0",
  "inference_p50_ms": 21.4, "inference_p99_ms": 38.9,
  "dropped_frames_pct": 0.02,
  "power_w_avg": 33.1, "soc_temp_c": 71.2, "throttled_pct": 0.0,
  "detections_per_km": {"personnel": 0.31, "machine": 2.04, "hazard": 1.77},
  "mean_confidence": 0.83, "ood_rate": 0.011,
  "safety_envelope_violations": 0,
  "disengagements": 0
}
```

An Azure Monitor rule evaluates a rollback predicate per ring. Any of:

- `inference_p99_ms > 45` for 3 consecutive windows
- `dropped_frames_pct > 0.5`
- `throttled_pct > 5`
- `detections_per_km.personnel` deviating > 3σ from the ring baseline (either direction —
  a collapse means blindness, a spike means nuisance stops)
- `ood_rate > 5×` the evaluation-set rate
- Any `disengagement` attributable to perception
- Any `safety_envelope_violation`

triggers **automatic rollback**: the rollback driver
([`deploy/rollout/rollback.py`](../deploy/rollout/rollback.py)) patches the affected twins'
`desired.bundle` back to the last-known-good, which the previously-cached bundle satisfies
without a download. Recovery is bounded by the twin propagation and module reload time —
seconds to a couple of minutes — not by a redeploy.

**Last-known-good is always resident on disk.** Robots retain the previous bundle. Rollback
must never depend on a network the robot may not have.

## 4.5 Closing the loop

The robot is not a passive consumer. Each `perception` inference feeds the `curator`
scoring described in [`docs/02-data-plane.md`](02-data-plane.md), and each new bundle carries
a fresh dataset centroid and threshold set. The fleet's definition of "interesting" is
updated with every release, which is what keeps the flywheel from re-collecting data it
already has.

Two shadow-mode outputs feed back with especially high value:

1. **Disagreement frames** — where the new and incumbent models differ. These are, almost by
   construction, the frames on the decision boundary, and they are the cheapest high-value
   labeling queue the system produces.
2. **OOD spikes** — a cluster of high-OOD frames from one site usually means a physical
   change (new equipment, a re-muck, a changed lighting install) that the dataset has never
   seen. This is the earliest possible warning of drift, and it arrives shift-by-shift rather
   than in a monthly report.
