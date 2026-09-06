# CareAgents container image (Railway / any OCI host).
#
# Build context is the REPO ROOT (careagents imports from the repo package),
# so deploy with:  railway up  from a staging dir, or set the service's
# Dockerfile path to deploy/careagents/Dockerfile with root as context.
#
# The image supports two process roles, selected at start-up by CARE_ROLE:
# ``web`` (the default) serves the app, and ``worker`` runs the durable
# inference/tool worker. Railway's `railway add` has no start-command option,
# so the role has to be reachable through the environment alone (#273).

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

# One image, two roles. `exec` so the role process replaces the shell and
# receives the platform's SIGTERM directly: gunicorn drains, and
# careagents.worker stops its pool through the handler it installs — neither
# happens if a shell sits between them as PID 1. An unrecognised role exits
# rather than falling through to web: a typo'd worker service that quietly
# started a second web server would report itself healthy while nothing ever
# drained the run queue. `${CARE_ROLE-web}`, not `${CARE_ROLE:-web}`, because
# only an unset variable means "default" — an empty one is a broken config.
# `printf`, not `echo`: dash's echo expands backslash escapes, so a role value
# containing one would truncate its own error message.
#
# `--threads 8` is a measurement, not a preference. Every open chat turn holds
# one thread for the whole life of its run (the SSE replay loop), so this pool
# is the concurrency ceiling for EVERY route, /healthz included. Measured
# 2026-09-03 against this exact invocation: at 2x4, eight concurrent turns made
# /healthz wait 18.19s and sixteen made it wait 38.32s; at 2x8, eight waited
# 0.00s and sixteen waited 18.23s
# (docs/evidence/2026-09-03-probe-219-thread-saturation.md §7, PR #573).
# One doubling is all the evidence supports: it moves the failure from 8
# concurrent turns to 16 rather than removing it, and each thread also polls
# HealthClaw ~4x/s on a browser's behalf — so more threads is not free, it is
# more load on the engine that also serves clinicians. Change this with a new
# measurement. Pinned by tests/test_careagents_container_roles.py.
CMD ["sh", "-c", "case \"${CARE_ROLE-web}\" in \
  web) exec gunicorn careagents.wsgi:app \
  --bind 0.0.0.0:${PORT:-8600} --workers ${CARE_WEB_WORKERS:-2} --threads 8 --timeout 180 \
  --access-logfile - --error-logfile - \
  --access-logformat '%(h)s \"%(r)s\" %(s)s %(M)sms' ;; \
  worker) exec python -m careagents.worker ;; \
  *) printf \"careagents: unknown CARE_ROLE '%s' (expected web or worker)\\n\" \"$CARE_ROLE\" >&2; exit 2 ;; \
esac"]
