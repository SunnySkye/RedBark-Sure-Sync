FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REDBARK_SURE_SYNC_IMAGE_HINT=ghcr.io/sunnyskye/redbark-sure-sync:latest

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY audit_redbark_to_sure_duplicates.py ./
COPY docker_entrypoint.py ./
COPY generate_account_map.py ./
COPY orchestrate_redbark_sync.py ./
COPY redbark_export_transactions.py ./
COPY sure_export_transactions.py ./
COPY sync_redbark_to_sure.py ./
COPY README.md ./
COPY LLM_CONTEXT.md ./
COPY LICENSE ./

RUN mkdir -p /app/exports /app/sure_exports /app/logs

ENTRYPOINT ["python", "docker_entrypoint.py"]