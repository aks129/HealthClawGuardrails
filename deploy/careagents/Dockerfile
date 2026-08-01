# CareAgents container image (Railway / any OCI host).
#
# Build context is the REPO ROOT (careagents imports from the repo package),
# so deploy with:  railway up  from a staging dir, or set the service's
# Dockerfile path to deploy/careagents/Dockerfile with root as context.
#
# One worker on purpose: chat history is process-local (careagents/wsgi.py),
# so a second worker would serve half the turns from an empty history.
# Threads give concurrency without splitting that state. Scaling past one
# process requires moving history to shared storage first.

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

COPY careagents/ ./careagents/

# Railway injects PORT; default keeps local `docker run` working.
ENV PORT=8600
EXPOSE 8600

# Health check hits the app's own readiness endpoint, so a container that
# boots but can't reach its database is reported unhealthy rather than "up".
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8600')+'/healthz', timeout=4).status==200 else 1)"

CMD ["sh", "-c", "gunicorn careagents.wsgi:app \
  --bind 0.0.0.0:${PORT:-8600} --workers 1 --threads 8 --timeout 120 \
  --access-logfile - --error-logfile - \
  --access-logformat '%(h)s \"%(r)s\" %(s)s %(M)sms'"]
