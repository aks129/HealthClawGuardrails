FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir uv

# Install Python dependencies (copy lockfile first for layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Add venv to PATH so gunicorn and other scripts are directly accessible
ENV PATH="/app/.venv/bin:$PATH"

# Copy application code
COPY . .

# Create instance directory for SQLite
RUN mkdir -p /app/instance

EXPOSE 5000

# Reap Fasten jobs stranded by the restart that just happened, then serve.
# Ingest runs in daemon threads inside this process, so every deploy kills
# whatever was importing; the reaper re-triggers a fresh export for jobs left
# non-terminal. Its own 5-minute ZOMBIE_MIN_AGE keeps a rolling deploy from
# double-triggering a job the outgoing container is still running.
#
# `;` and not `&&`: recovery is best effort, serving is not. If the reaper
# exits non-zero — an unreachable database at boot, say — the app must still
# come up, or a convenience would have become an outage with a restart loop
# behind it. Not in create_app: tests/test_app_factory.py pins the factory as
# side-effect-free, and that is worth more than the convenience.
CMD flask --app main recover-zombies; \
    gunicorn main:app --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 300 --keep-alive 5
