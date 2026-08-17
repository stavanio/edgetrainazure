# Curation environment. CPU only -- the work here is IO- and decode-bound, and
# putting it on GPU nodes would idle expensive silicon behind blob reads.
FROM mcr.microsoft.com/azureml/openmpi4.1.0-ubuntu22.04@sha256:0000000000000000000000000000000000000000000000000000000000000000

ARG PRIVATE_FEED_URL

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pipelines/aml/environments/curate-requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir \
        --index-url "${PRIVATE_FEED_URL}" \
        --require-hashes \
        -r /tmp/requirements.txt \
    && rm -rf /root/.cache/pip /tmp/requirements.txt

COPY src/ /opt/edgeforge/src/
ENV PYTHONPATH=/opt/edgeforge/src:$PYTHONPATH

RUN python -c "import edgeforge.curation.quality_gates, edgeforge.curation.dedupe_and_sample; \
    print('edgeforge curate env ok')"

WORKDIR /opt/edgeforge
