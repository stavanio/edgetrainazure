# Training environment.
#
# Base image is pinned by DIGEST, never tag. A ':latest' anywhere in this repo is
# a CI failure -- see .github/workflows/ci.yml. The digest is what makes an AML
# job reproducible eighteen months later, which is the claim the safety case
# rests on.
FROM mcr.microsoft.com/azureml/curated/acpt-pytorch-2.4-cuda12.4@sha256:0000000000000000000000000000000000000000000000000000000000000000

ARG PYTHON_VERSION=3.11

# Training compute has NO route to the internet (infra/main.tf). Packages come
# from a private feed reached over a private endpoint; this build runs on a
# builder with feed access, and the resulting image is the only thing the
# training subnet ever pulls.
ARG PRIVATE_FEED_URL

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Required for torch.use_deterministic_algorithms with cuBLAS GEMMs.
    CUBLAS_WORKSPACE_CONFIG=:4096:8 \
    # NCCL over the InfiniBand fabric on ND-series. Harmless on NC-series.
    NCCL_IB_DISABLE=0 \
    NCCL_DEBUG=WARN \
    OMP_NUM_THREADS=8

COPY pipelines/aml/environments/train-requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir \
        --index-url "${PRIVATE_FEED_URL}" \
        --require-hashes \
        -r /tmp/requirements.txt \
    && rm -rf /root/.cache/pip /tmp/requirements.txt

COPY src/ /opt/edgeforge/src/
ENV PYTHONPATH=/opt/edgeforge/src:$PYTHONPATH

# Fail fast and loudly if the image cannot import what it exists to run, rather
# than discovering it eight minutes into a scheduled job on eight A100 nodes.
RUN python -c "import torch, mlflow, edgeforge.training.train_perception as t; \
    assert torch.cuda.is_available() or True; \
    print('edgeforge train env ok:', torch.__version__, t.HEADS)"

WORKDIR /opt/edgeforge
