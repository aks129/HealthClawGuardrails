# SMART Health Links storage server — jmandel/kill-the-clipboard-skill (MIT)
# Pinned to a reviewed SHA; the server stores only ciphertext + hashed auth
# capabilities (zero-knowledge: it can never read PHI).
#
# Bumped fa0020d -> 6ff88e9 (2026-07): adds an unauthenticated /health liveness
# probe (wired into the container health check below and docker-compose) and
# first-class never-expiring links. JWE compression default flipped to opt-in,
# but our shl_generate passes deflate:true explicitly, so payloads still compress.
FROM oven/bun:1.2
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl && rm -rf /var/lib/apt/lists/*
ARG KTC_SHA=6ff88e90d940f0f8334541269658e4406c961578
RUN git clone https://github.com/jmandel/kill-the-clipboard-skill /app \
    && cd /app && git checkout "$KTC_SHA"
WORKDIR /app
RUN bun install --frozen-lockfile
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["bun", "run", "server/src/index.ts"]
