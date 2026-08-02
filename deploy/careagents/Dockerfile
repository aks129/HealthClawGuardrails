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
CMD ["sh", "-c", "case \"${CARE_ROLE-web}\" in \
  web) exec gunicorn careagents.wsgi:app \
  --bind 0.0.0.0:${PORT:-8600} --workers ${CARE_WEB_WORKERS:-2} --threads 4 --timeout 180 \
  --access-logfile - --error-logfile - \
  --access-logformat '%(h)s \"%(r)s\" %(s)s %(M)sms' ;; \
  worker) exec python -m careagents.worker ;; \
  *) printf \"careagents: unknown CARE_ROLE '%s' (expected web or worker)\\n\" \"$CARE_ROLE\" >&2; exit 2 ;; \
esac"]
