# SMART Health Links storage server — jmandel/kill-the-clipboard-skill (MIT)
# Pinned to a reviewed SHA; the server stores only ciphertext + hashed auth
# capabilities (zero-knowledge: it can never read PHI).
FROM oven/bun:1.2
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
ARG KTC_SHA=fa0020dcc03e5daa2470462c404a1e69c637b474
RUN git clone https://github.com/jmandel/kill-the-clipboard-skill /app \
    && cd /app && git checkout "$KTC_SHA"
WORKDIR /app
RUN bun install --frozen-lockfile
EXPOSE 8000
CMD ["bun", "run", "server/src/index.ts"]
