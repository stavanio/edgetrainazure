
# edgeforge -- thin wrappers over `az ml` / `az iot`. Nothing is hidden behind
# bespoke tooling: every target prints a command you could have typed.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

-include .env
export

AZ_RESOURCE_GROUP ?= rg-edgefrg-dev
AZ_WORKSPACE      ?= mlw-edgefrg-dev
AZ_REGISTRY       ?= mlr-edgefrg
AZ_IOTHUB         ?= iot-edgefrg-dev
AZ_ACR            ?= acredgefrgdev.azurecr.io
AZ_GRAFANA        ?= graf-edgefrg-dev

AML := az ml --resource-group $(AZ_RESOURCE_GROUP) --workspace-name $(AZ_WORKSPACE)
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

MODEL   ?= hazard-seg
DATASET ?= mr1-hazard:12
TARGET  ?= orin-agx-64
RING    ?= canary

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nedgeforge targets:\n\n"} \
	  /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 } \
	  /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
	@echo ""

##@ Setup

.PHONY: register-aml
register-aml: ## Register environments, components, and the scenario taxonomy
	$(AML) environment create -f pipelines/aml/environments/train.yml
	$(AML) environment create -f pipelines/aml/environments/eval.yml
	$(AML) environment create -f pipelines/aml/environments/curate.yml
	@echo "registered environments at commit $(GIT_SHA)"

.PHONY: register-observability
register-observability: ## Apply SLO dashboards to Managed Grafana
	@for d in observability/dashboards/*.json; do \
	  echo "applying $$d"; \
	  az grafana dashboard update -n $(AZ_GRAFANA) --definition "@$$d" --overwrite true >/dev/null; \
	done
	@echo "dashboards applied; edit them in git, not in the UI (editable=false)"

.PHONY: env
env: ## Print the .env block to copy from terraform output
	terraform -chdir=infra output -raw make_env

##@ Data

.PHONY: curate
curate: ## Curate a raw drop.  make curate RAW_URI=azureml://...
	@test -n "$(RAW_URI)" || { echo "RAW_URI is required"; exit 1; }
	$(AML) job create -f pipelines/aml/pipeline-data-curation.yml \
	  --set inputs.raw_uri.path=$(RAW_URI) \
	  --set tags.git_sha=$(GIT_SHA) \
	  --web

.PHONY: snapshot
snapshot: ## Cut a frozen, content-addressed dataset snapshot
	@test -n "$(DATASET)" || { echo "DATASET is required"; exit 1; }
	python -m edgeforge.curation.run_snapshot \
	  --dataset $(basename $(DATASET)) \
	  --note "$(NOTE)" \
	  --register-data-asset \
	  --fail-on-safety-critical-shortfall
	@echo ""
	@echo ">> Read the coverage report before training. A cell below its floor"
	@echo ">> here becomes a failed slice gate five days from now."

##@ Training

.PHONY: train
train: ## Train + evaluate.  make train MODEL=hazard-seg DATASET=mr1-hazard:12
	$(AML) job create -f pipelines/aml/pipeline-train-perception.yml \
	  --set inputs.snapshot.path=azureml:$(DATASET) \
	  --set tags.model=$(MODEL) \
	  --set tags.git_sha=$(GIT_SHA) \
	  $(if $(SWEEP),--set settings.default_compute=azureml:gpu-sweep,) \
	  --web

.PHONY: gate-report
gate-report: ## Explain a gate failure.  make gate-report RUN=<aml_run_id>
	@test -n "$(RUN)" || { echo "RUN is required"; exit 1; }
	python -m edgeforge.evaluation.report --run $(RUN) --html outputs/gate-report.html
	@echo "wrote outputs/gate-report.html -- start with the failing slice, not the aggregate"

##@ Edge

.PHONY: edge-bundle
edge-bundle: ## Optimize + package + sign on the HIL rack (~35 min)
	@test -n "$(VERSION)" || { echo "VERSION is required"; exit 1; }
	gh workflow run edge-release.yml \
	  -f model=$(MODEL) -f version=$(VERSION) -f target=$(TARGET) -f ring=hil

.PHONY: rollout
rollout: ## Roll a bundle to a ring.  make rollout BUNDLE=hazard-seg:41 RING=canary
	@test -n "$(BUNDLE)" || { echo "BUNDLE is required"; exit 1; }
	python deploy/rollout/rollout.py \
	  --hub $(AZ_IOTHUB) \
	  --bundle $(BUNDLE) \
	  --reference $(shell python -m edgeforge.packaging.cli resolve --bundle $(BUNDLE) --target $(TARGET) --registry $(AZ_ACR)) \
	  --ring $(RING) \
	  --release-id manual-$(GIT_SHA)

.PHONY: rollback
rollback: ## Roll a ring back.  make rollback RING=canary [BUNDLE=hazard-seg:40]
	python deploy/rollout/rollback.py \
	  --hub $(AZ_IOTHUB) --ring $(RING) \
	  $(if $(BUNDLE),--bundle $(BUNDLE),) \
	  --reason "$(or $(REASON),manual)"

.PHONY: rollout-freeze
rollout-freeze: ## Halt all ring advancement fleet-wide (incident step 1)
	az iot hub device-twin update -n $(AZ_IOTHUB) --device-id '*' \
	  --set 'tags.rollout_frozen=true'
	@echo "fleet rollout FROZEN. Clear with: make rollout-thaw"

.PHONY: rollout-thaw
rollout-thaw: ## Clear the fleet-wide rollout freeze
	az iot hub device-twin update -n $(AZ_IOTHUB) --device-id '*' \
	  --set 'tags.rollout_frozen=false'

.PHONY: twin-patch
twin-patch: ## Retune fleet curiosity without a deployment
	@test -n "$(KEY)" -a -n "$(VALUE)" || { echo "KEY and VALUE are required"; exit 1; }
	az iot hub device-twin update -n $(AZ_IOTHUB) \
	  --query "[?tags.ring=='$(RING)'].deviceId" \
	  --set 'properties.desired.$(KEY)=$(VALUE)'
	@echo "patched $(KEY)=$(VALUE) on ring $(RING); effective next shift"

##@ Observability

.PHONY: slo-report
slo-report: ## Evaluate every SLI and print the release posture (exit 0/1/2)
	python -m edgeforge.fleet.report --workspace-id $(AZ_LOG_ANALYTICS_WORKSPACE_ID) $(if $(GROUP),--group $(GROUP),)

.PHONY: slo-publish
slo-publish: ## Evaluate and write results to SloStatus_CL for the dashboards
	python -m edgeforge.fleet.report --workspace-id $(AZ_LOG_ANALYTICS_WORKSPACE_ID) --publish

.PHONY: slo-gate
slo-gate: ## CI gate: fail if the error-budget policy forbids shipping
	@python -m edgeforge.fleet.report --workspace-id $(AZ_LOG_ANALYTICS_WORKSPACE_ID) --json > outputs/slo.json; \
	  code=$$?; \
	  if [ $$code -eq 2 ]; then echo "::error::release posture is FREEZE"; fi; \
	  exit $$code

##@ Development

.PHONY: test
test: ## Run unit tests
	pytest -q --cov=edgeforge --cov-report=term-missing

.PHONY: lint
lint: ## Lint, format check, and type check
	ruff check src deploy tests
	ruff format --check src deploy tests
	mypy src/edgeforge
	python observability/validate.py

.PHONY: fmt
fmt: ## Auto-format
	ruff format src deploy tests
	ruff check --fix src deploy tests
	terraform -chdir=infra fmt -recursive

.PHONY: plan
plan: ## Terraform plan
	terraform -chdir=infra plan
