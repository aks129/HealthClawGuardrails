# CareAgents container image (Railway / any OCI host).
#
# Build context is the REPO ROOT (careagents imports from the repo package),
# so deploy with:  railway up  from a staging dir, or set the service's
# Dockerfile path to deploy/careagents/Dockerfile with root as context.
#
# The image supports two process roles: the default web command below, and
# ``python -m careagents.worker`` for the durable inference/tool worker.

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Lockfile first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV CARE_ROLE=web

COPY careagents/ ./careagents/

# Railway injects PORT; default keeps local `docker run` working.
ENV PORT=8600
EXPOSE 8600

# The web role probes its readiness endpoint. The worker role checks durable
# queue presence, so a live-but-hung process cannot advertise itself healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -m careagents.healthcheck

CMD ["sh", "-c", "gunicorn careagents.wsgi:app \
  --bind 0.0.0.0:${PORT:-8600} --workers ${CARE_WEB_WORKERS:-2} --threads 4 --timeout 180 \
  --access-logfile - --error-logfile - \
  --access-logformat '%(h)s \"%(r)s\" %(s)s %(M)sms'"]
