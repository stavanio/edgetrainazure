# 05 — From the AWS reference to Azure

This repo is the Azure realization of an AWS-based robot MLOps ecosystem. Most of the
mapping is mechanical. The parts that are not are called out in §5.3 — those are where
migrations actually go wrong.

## 5.1 Service mapping

| Capability | AWS reference | Azure here | Fidelity |
|---|---|---|---|
| Device identity & connectivity | IoT Core | **IoT Hub** + **DPS** | ≈ equal |
| Zero-touch provisioning | IoT Core fleet provisioning | **DPS** with TPM / X.509 attestation | Azure stronger (TPM-native) |
| Device security posture | IoT Device Defender | **Microsoft Defender for IoT** | ≈ equal |
| Edge runtime | IoT Greengrass v2 | **Azure IoT Edge** | ≈ equal, different component model |
| Edge runtime (heavy robots) | Greengrass + ECS Anywhere | **Arc-enabled Kubernetes** | Azure stronger |
| OTA / job orchestration | IoT Jobs, Greengrass deployments | **IoT Edge layered deployments** + device twins | Azure stronger for partial updates |
| Object storage | S3 | **ADLS Gen2** (HNS on) | Azure stronger for analytics paths |
| Immutable evidence store | S3 Object Lock | **Immutable blob policy** + legal hold | ≈ equal |
| Bulk physical transfer | Snowball / Snowcone | **Azure Data Box** | ≈ equal |
| Streaming ingest | Kinesis Data Streams | **Event Hubs** | ≈ equal |
| Stream processing | Kinesis Analytics | **Stream Analytics** | ≈ equal |
| ETL / big data | Glue, EMR | **Azure Databricks** (or Fabric) | Azure stronger — Delta + Unity Catalog |
| Table format | Iceberg / Hudi on S3 | **Delta Lake** | Different, equivalent capability |
| Data catalog / governance | Glue Catalog + Lake Formation | **Unity Catalog** + **Microsoft Purview** | ≈ equal |
| Labeling | SageMaker Ground Truth | **AML Data Labeling** | AWS slightly ahead on managed workforce |
| Managed labeling workforce | Ground Truth + Mechanical Turk | Partner/vendor integration | **Gap — see §5.3** |
| Experiment tracking | SageMaker Experiments | **MLflow in AML** (native) | Azure stronger — open standard |
| Training jobs | SageMaker Training | **AML command / sweep jobs** | ≈ equal |
| HPO | SageMaker AMT | **AML sweep** (bandit / median stopping) | ≈ equal |
| Pipeline orchestration | SageMaker Pipelines, Step Functions | **AML Pipelines**, Logic Apps | ≈ equal |
| Model registry | SageMaker Model Registry | **AML Registry** (cross-workspace) | Azure stronger for multi-env promotion |
| Feature store | SageMaker Feature Store | **AML managed feature store** | ≈ equal (not used here) |
| Online inference | SageMaker endpoints | **AML managed online endpoints** | ≈ equal |
| Edge model compilation | **SageMaker Neo** | *No equivalent* — HIL rack + TensorRT | **Gap — see §5.3** |
| Container registry | ECR | **ACR Premium**, geo-replicated | Azure stronger — geo-replication, OCI artifacts |
| Multi-arch builds | CodeBuild + buildx | **ACR Tasks** + buildx | ≈ equal |
| Artifact signing | Signer + Notation | **Notation** + Key Vault HSM | ≈ equal |
| Secrets | Secrets Manager | **Key Vault** | ≈ equal |
| Key management | KMS | **Key Vault / Managed HSM** | ≈ equal |
| Workload identity | IAM roles for service accounts | **Managed identity** / workload identity federation | Azure stronger (no key material at all) |
| CI/CD | CodePipeline / CodeBuild | **GitHub Actions** (+ Azure DevOps) | ≈ equal |
| Metrics / logs | CloudWatch | **Azure Monitor** + **Log Analytics** | Azure stronger — KQL |
| Dashboards | CloudWatch Dashboards / Managed Grafana | **Azure Managed Grafana** | ≈ equal |
| APM | X-Ray | **Application Insights** | Azure stronger |
| Batch compute | AWS Batch | **Azure Batch** | ≈ equal |
| Robotics simulation | **RoboMaker** (deprecated) | Isaac Sim on Azure Batch GPU | Neither has it; parity by self-hosting |
| Spot / preemptible | Spot Instances | **Spot VMs** / AML low-priority | ≈ equal |
| Private networking | PrivateLink | **Private Endpoints** / Private Link | ≈ equal |
| Policy / guardrails | SCPs, Config | **Azure Policy**, management groups | Azure stronger |

## 5.2 Conceptual translations

Four ideas do not map name-to-name and need translating in your head:

**IAM role → managed identity.** An AWS role is assumed; an Azure managed identity is
*attached* to a resource and never has credentials at all. In practice this simplifies the
migration: most `sts:AssumeRole` plumbing disappears rather than being ported.

**S3 bucket policy → RBAC + ACL.** ADLS Gen2 has both Azure RBAC (coarse, at container
scope) and POSIX ACLs (fine, at path scope). The AWS habit of expressing everything in one
bucket policy splits into two mechanisms here. Use RBAC for the 90% case; reserve POSIX ACLs
for the golden-set isolation described in [`docs/03-training-plane.md`](03-training-plane.md).

**Greengrass component → IoT Edge module.** Greengrass components are recipes with arbitrary
lifecycle scripts; IoT Edge modules are containers with a fixed lifecycle plus twins. Modules
are more constrained and considerably easier to reason about. Anything relying on Greengrass
component *lifecycle hooks* needs redesign into module init.

**IoT Jobs → device twins.** The largest mental shift. AWS Jobs are imperative and
per-device ("go do this"). Azure twins are declarative and desired-state ("you should be in
this state"). Rollout, rollback, and ring management all get *simpler*: state is
convergent, so a robot that was offline during a rollback converges to the correct state
when it reconnects rather than needing the job replayed.

## 5.3 Where the mapping breaks

Three real gaps. Two are handled here; one is a standing operational cost.

### Gap 1 — SageMaker Neo has no Azure equivalent

Neo compiles and optimizes a model for a target edge device as a managed service. Azure has
no counterpart.

*Handled by:* the HIL rack in [`docs/04-edge-plane.md`](04-edge-plane.md). Real target modules,
Arc-enabled, running as self-hosted GitHub Actions runners, building and measuring every
engine.

*Honest assessment:* this is more infrastructure than Neo required — a rack to buy, house,
and maintain. It is also **strictly better**, because you get measured p99 latency, real
sustained power, and real thermal behaviour on production hardware instead of a compiled
artifact and an assumption. For a safety-relevant robot, you would want the rack even if
Neo were available. The cost is real; the outcome is superior.

### Gap 2 — no managed labeling workforce

Ground Truth's integration with a vendor and public workforce marketplace has no direct
Azure analogue.

*Handled by:* AML Data Labeling projects with a private workforce (site personnel and a
contracted vendor), federated into Entra ID as B2B guests with conditional access. For this
workload it is arguably the right answer anyway — underground hazard classes need domain
knowledge that a general crowd workforce does not have, and mine imagery has data-residency
constraints that rule out a public marketplace.

*Cost:* vendor management and annotator onboarding become your problem. Budget for it.

### Gap 3 — RoboMaker is gone and Azure never had it

AWS deprecated RoboMaker; Azure has no managed robotics simulation service.

*Handled by:* self-hosted Isaac Sim on Azure Batch spot GPU pools ([`sim/`](../sim)). This is
now the standard approach on both clouds, so it is not a migration penalty — it is just where
the industry landed.

## 5.4 Migration order

If porting an existing AWS pipeline rather than building fresh, this order minimizes
the window where two systems are both authoritative:

1. **Storage and lake first.** Stand up ADLS + Delta, dual-write from the existing ETL.
   Backfill with AzCopy or Data Factory. Verify row counts and content hashes match.
2. **Training second.** AML can read from either lake during transition. Port pipelines,
   re-run the last three known models, confirm metrics reproduce within tolerance. Do not
   move on until they do.
3. **Registry and evaluation third.** Move gates and golden set. Run both registries in
   parallel for one release cycle.
4. **Edge last, and slowly.** Greengrass → IoT Edge is the highest-risk step because it
   touches robots. Convert one robot, then the HIL rack, then a canary ring, then a site.
   Robots can run either runtime during transition — they are independent populations.
5. **Decommission** only after one full retraining cycle has completed end to end on Azure
   and a rollback has been rehearsed on the new stack.

The ingest path is deliberately migrated *first* and cut over *last*: dual-writing raw data
to both clouds is cheap insurance, and raw data is the only thing in this system that cannot
be regenerated.
