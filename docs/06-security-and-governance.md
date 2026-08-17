# 06 — Security and governance

The threat model here is not "someone reads our training data." It is **"someone changes
what a 30-tonne machine believes it is looking at."** Every control below is ordered by
that.

## 6.1 Threat model

| # | Threat | Impact | Control |
|---|---|---|---|
| T1 | Unauthorized model on a robot | Catastrophic — arbitrary perception behaviour | Notation signature verified on device; unsigned bundle refused |
| T2 | Poisoned training data | Severe — latent, triggered failure | Immutable `/raw`, device-attested provenance, label audit, slice gates |
| T3 | Compromised build pipeline | Catastrophic | SLSA provenance, HSM signing keys, protected branches, 2-person promotion |
| T4 | Stolen device identity | Moderate — false data injection | TPM-attested DPS enrollment, per-device X.509, short-lived SAS only |
| T5 | Model exfiltration | Moderate — IP loss | Private endpoints, no public egress, ACR content trust, encrypted at rest |
| T6 | Test-set leakage | Severe — invalid safety evidence | Golden set in a separate container, IAM-denied to training compute |
| T7 | Twin manipulation | Severe — forced bad rollout | Twin write restricted to the rollout service principal; all patches audited |
| T8 | PII exposure | Regulatory | Blur before human access; unblurred originals access-controlled and logged |

## 6.2 Identity — nothing has a password

There are no connection strings, storage keys, or service-principal secrets anywhere in this
repository or in any deployed configuration.

| Workload | Identity | Grants |
|---|---|---|
| AML training compute | User-assigned MI `mi-train` | Read `/snapshot`, `/labeled`; write MLflow; **explicit Deny on golden-set container** |
| AML eval compute | User-assigned MI `mi-eval` | Read golden set; read models; write metrics |
| Databricks | MI via Unity Catalog storage credential | Read `/raw`; read-write `/clean`, `/curated` |
| SAS broker Function | System-assigned MI | User-delegation key on `/raw` only, path-scoped |
| GitHub Actions | Workload identity federation (OIDC) | No stored secret; per-env federated credentials |
| HIL runners | Arc-enabled server MI | Pull ACR; push bundles; sign via Key Vault |
| Rollout driver | MI `mi-rollout` | IoT Hub twin write; ACR read |
| Robot | X.509 leaf, TPM-bound, per device | IoT Hub D2C; SAS request only |

Human access is Entra ID PIM — no standing privilege on `prod`. Elevation is time-boxed,
justified, approved, and logged.

## 6.3 Network

- Every PaaS service is reached over a **Private Endpoint**. Public network access is
  disabled on the storage accounts, Key Vault, ACR, and the AML workspace.
- The AML training subnet has **no outbound internet route**. Package installation is from a
  private feed; base images come from ACR. A training job cannot phone home, by construction.
- IoT Hub is the *only* internet-facing ingress, and it accepts only mutually-authenticated
  TLS 1.2+ with per-device certificates.
- The SAS broker is the only other externally-reachable surface. It is a Function App behind
  a Front Door WAF, and it issues nothing but path-scoped, ≤15-minute, write-only
  user-delegation SAS tokens for `/raw`.

## 6.4 Supply chain

```
source → build → sign → verify → run
  │        │       │       │        │
  │        │       │       │        └─ device: notation verify against pinned trust store
  │        │       │       └────────── rollout: verify before twin patch
  │        │       └────────────────── Key Vault Premium, non-exportable HSM key
  │        └────────────────────────── ACR Tasks, SLSA provenance attestation
  └─────────────────────────────────── protected branch, signed commits, CODEOWNERS
```

- **Base images pinned by digest**, never tag. A `:latest` anywhere is a CI failure.
- **SBOM generated per bundle** (SPDX), stored as an OCI referrer, scanned by Defender for
  Containers. A new critical CVE in a shipped bundle raises a fleet work item automatically.
- **Signing keys never leave the HSM.** The build runner calls Key Vault to sign; it cannot
  read the key.
- **Trust store on the robot is pinned at manufacture** and rotated only through a signed
  base-deployment update, which is itself a two-person change.

## 6.5 Data governance

**Residency.** Each site's `/raw` is written to a storage account in the geography that site
requires. Curation and training run in-geo. Only model weights — which contain no
recoverable imagery — cross geographies, and only after a documented review.

**PII.** Faces and identifying markings are blurred irreversibly at `/curated`. Unblurred
`/raw` access requires PIM elevation with a stated purpose; every read is logged to an
immutable audit table. Works-council agreements at EU sites are satisfied by the fact that
the routine ML workflow *never* touches unblurred data.

**Right to erasure.** Personnel can request removal. `/raw` immutability and erasure conflict;
resolved by cryptographic erasure — per-subject keys for the identifying crops, destroyed on
request, leaving the frame present but the identity unrecoverable. Documented in the DPIA.

**Lineage.** Purview plus AML Registry answers, for any deployed model, in one query:
which snapshot, which frames, which labelers, which code SHA, which environment digest,
which gates, which approvers, which robots ran it and when. This is not a nice-to-have —
it is the artifact an incident investigation or a regulator asks for.

## 6.6 Safety-adjacent governance

`edgeforge` does not make the safety case. It produces evidence for one.

| Evidence | Produced by | Where |
|---|---|---|
| Dataset adequacy vs. ODD | Taxonomy coverage report | `curation.snapshot` |
| Performance per operating condition | Slice gate table | `evaluation/gates.py` |
| Decision-level behaviour | Closed-loop replay scores | `evaluation/closed_loop.py` |
| Known limitations | Model card | `packaging/model_card.py` |
| Field performance | Fleet telemetry, drift monitors | Log Analytics |
| Change control | Registry stage transitions + approvals | AML Registry |
| Rollback capability | Rehearsed quarterly, recorded | `deploy/rollout/` |

Relevant frameworks: **ISO 17757** (earth-moving machinery autonomy), **IEC 61508**
(functional safety), **ISO 12100** (risk assessment), **ISO/IEC 42001** (AI management
systems), and the **EU AI Act** high-risk obligations where applicable. The gates in this
repo are designed so that passing them generates the documentation those frameworks ask for
as a side effect, rather than as a separate manual exercise.

## 6.7 Auditability

Everything that changes fleet behaviour is logged immutably and correlatable by a single
`release_id`:

- Registry stage transitions, with approver identities
- Twin patches, with the principal and prior value
- Bundle signatures and verification results, per device
- Module restarts, engine loads, rollbacks
- Every gate result, pass or fail, including the failures that never shipped

Retained 7 years in a Log Analytics workspace with an immutable export to `/raw`-class
storage. The failures matter as much as the successes: "we tried this model and it did not
pass the personnel slice" is exactly the record that demonstrates the process works.
