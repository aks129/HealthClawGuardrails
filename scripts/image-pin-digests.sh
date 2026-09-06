#!/usr/bin/env bash
# What the example's compose pins resolve to TODAY, against the 2026-08-16 baseline.
#
# Why this exists: §7 of `docs/evidence/2026-08-16-set2-connectors.md` recorded
# four manifest digests and said plainly that "do they still resolve to the
# digest they were pinned at" could not be answered, because no baseline had
# ever been recorded — so those four digests were "the baseline for the next
# run". This is the next run, and unlike §5 and §6 that section named no script
# at all: it says only "resolved today via the registry HTTP API". This script
# is that sentence, written down (#602).
#
# It reads the image refs OUT of the compose file rather than hard-coding them,
# so a change to the compose file changes what is measured. The baselines below
# are hard-coded, because they are a historical record of one day and must not
# follow anything.
#
# Reads only. No pull, no `docker` (the 2026-08-16 pass had no daemon either),
# nothing created anywhere.
#
# Usage:
#   scripts/image-pin-digests.sh
#   scripts/image-pin-digests.sh path/to/other-compose.yaml     # negative control
set -uo pipefail

COMPOSE="${1:-examples/aidbox-healthclaw-guardrails/docker-compose.yaml}"

# The Accept header decides WHICH manifest the registry returns, and a
# multi-arch index and a single-platform manifest have different digests. Send
# a different set from the 2026-08-16 run and every row reads "changed" as an
# artifact of the request rather than a re-push. This set is printed in the
# output so the next run can send the same one.
ACCEPT='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'

# Baseline: docs/evidence/2026-08-16-set2-connectors.md §7, measured 2026-08-16.
# `moving` marks a tag the compose file itself does not pin — a change there is
# R9 happening, not a fault. A change on a `version` tag is a re-push under a
# tag the compose treats as immutable, which is a FAIL.
BASELINE_ghcr_guardrails='sha256:57b345e0c8f6a6bf88690084f57fe863bd02882ade0e2c7b70002baa4c0e225b'
BASELINE_ghcr_mcp='sha256:d37c997ea15c73c715cdc2b90ea01aa6a49dc34cb6897913ce1691d8d012cd53'
BASELINE_aidboxone='sha256:42e4e8e10d9d42b54bf3f4602b3f584e06acc70738cdcf280ce7634bdb5e58b3'
BASELINE_postgres='sha256:06cad38a5d9f5d24b4d83d86def30795d5e4b757fedbf5281172b576dedcd941'

# The two supporting observations §7 recorded alongside the table. They are
# re-read here rather than restated: a claim this script does not measure does
# not belong in its output.
BASELINE_TAGS='latest, v1.4.0, 1.5.0, 1.5, 1.6.0, 1.6, 1.7.0, 1.7, 1.8.0, 1.8, 1.9.0, 1.9, 1.10.0'

fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }
note() { printf '  \033[33mNOTE\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m%s\033[0m\n' "$*"; exit 2; }

[ -f "$COMPOSE" ] || die "no compose file at ${COMPOSE} (run from the repo root)"

printf 'image-pin-digests.sh\n'
printf 'compose  %s\n' "$COMPOSE"
printf 'date     %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
printf 'accept   %s\n' "$ACCEPT"
printf 'baseline docs/evidence/2026-08-16-set2-connectors.md §7 (2026-08-16)\n'

# --- registry plumbing ------------------------------------------------------

# Anonymous pull token. Both registries hand one out for a public repository;
# a failure here is a failure of the run, never a skipped row.
registry_token() {
  local host="$1" repo="$2" url
  case "$host" in
    ghcr.io)   url="https://ghcr.io/token?scope=repository:${repo}:pull&service=ghcr.io" ;;
    docker.io) url="https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repo}:pull" ;;
    *)         return 1 ;;
  esac
  curl -sS --max-time 30 "$url" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("token") or d.get("access_token") or "")' 2>/dev/null
}

registry_api() {
  case "$1" in
    ghcr.io)   echo "https://ghcr.io/v2" ;;
    docker.io) echo "https://registry-1.docker.io/v2" ;;
  esac
}

# Prints the manifest digest, or nothing. Never prints a digest it did not read
# from a Docker-Content-Digest header.
manifest_digest() {
  local host="$1" repo="$2" ref="$3" tok
  tok=$(registry_token "$host" "$repo") || return 1
  [ -z "$tok" ] && return 1
  curl -sSI --max-time 30 -H "Authorization: Bearer ${tok}" -H "Accept: ${ACCEPT}" \
       "$(registry_api "$host")/${repo}/manifests/${ref}" 2>/dev/null \
    | tr -d '\r' | awk 'tolower($1)=="docker-content-digest:"{print $2}'
}

tag_list() {
  local host="$1" repo="$2" tok
  tok=$(registry_token "$host" "$repo") || return 1
  [ -z "$tok" ] && return 1
  curl -sS --max-time 30 -H "Authorization: Bearer ${tok}" \
       "$(registry_api "$host")/${repo}/tags/list?n=200" 2>/dev/null \
    | python3 -c 'import sys,json;print(", ".join(json.load(sys.stdin).get("tags") or []))' 2>/dev/null
}

# `ghcr.io/aks129/x:1.10.0` -> host, repo, ref. Docker Hub short names get the
# `library/` prefix the API needs.
split_ref() {
  python3 - "$1" <<'PY'
import sys
ref = sys.argv[1]
name, _, tag = ref.rpartition(":")
if "/" not in tag and name:
    pass
else:  # no tag at all
    name, tag = ref, "latest"
head, _, rest = name.partition("/")
if "." in head or ":" in head or head == "localhost":
    host, repo = head, rest
else:
    host, repo = "docker.io", name if "/" in name else f"library/{name}"
print(host); print(repo); print(tag)
PY
}

# --- the four rows ----------------------------------------------------------

step "1. Manifest digests for every image the compose file names"

images=$(grep -E '^[[:space:]]*image:[[:space:]]*' "$COMPOSE" | sed -E 's/^[[:space:]]*image:[[:space:]]*//' | tr -d '\042\047')
[ -z "$images" ] && die "no image: lines in ${COMPOSE} — nothing was measured"

printf '  %-52s %s\n' "image" "digest today"
seen=0
# Which of §7's four rows this run actually compared. Without this the script
# passes vacuously when the compose file no longer names them: the first
# version of it reported "no assertion failed" against a compose holding one
# unrelated image, having compared nothing at all.
compared=""
for ref in $images; do
  seen=$((seen + 1))
  read -r host repo tag < <(split_ref "$ref" | paste -sd' ' -)
  digest=$(manifest_digest "$host" "$repo" "$tag")
  if [ -z "$digest" ]; then
    printf '  %-52s %s\n' "$ref" "(no digest)"
    bad "${ref}: the registry returned no Docker-Content-Digest. This run measured nothing for this row."
    continue
  fi
  printf '  %-52s %s\n' "$ref" "$digest"

  case "$ref" in
    ghcr.io/aks129/healthclaw-guardrails:1.10.0)  base="$BASELINE_ghcr_guardrails"; kindof=version ;;
    ghcr.io/aks129/healthclaw-mcp-server:1.10.0)  base="$BASELINE_ghcr_mcp";        kindof=version ;;
    healthsamurai/aidboxone:edge)                 base="$BASELINE_aidboxone";       kindof=moving  ;;
    postgres:18)                                  base="$BASELINE_postgres";        kindof=moving  ;;
    *)                                            base="";                          kindof=new     ;;
  esac

  [ -n "$base" ] && compared="${compared} ${ref}"

  if [ -z "$base" ]; then
    note "${ref}: not in the 2026-08-16 table, so there is nothing to compare it to."
  elif [ "$digest" = "$base" ]; then
    if [ "$kindof" = version ]; then
      ok "${ref}: same digest as 2026-08-16. The version tag was not re-pushed."
    else
      note "${ref}: same digest as 2026-08-16, which a moving tag is not obliged to be."
    fi
  else
    if [ "$kindof" = version ]; then
      bad "${ref}: MOVED since 2026-08-16 (${base} -> ${digest}). A version tag the compose treats as immutable was re-pushed."
    else
      note "${ref}: MOVED since 2026-08-16 (${base} -> ${digest}). This is R9: an unpinned tag, and \`pull_policy: always\` on one of them."
    fi
  fi
done
[ "$seen" -lt 1 ] && bad "no image rows were resolved at all"

for want in \
  'ghcr.io/aks129/healthclaw-guardrails:1.10.0' \
  'ghcr.io/aks129/healthclaw-mcp-server:1.10.0' \
  'healthsamurai/aidboxone:edge' \
  'postgres:18'
do
  case "$compared" in
    *" ${want}"*) ;;
    *) bad "${want} is in §7's table but was not compared this run — ${COMPOSE} no longer names it, so §7's question went unanswered for that row." ;;
  esac
done

# --- §7's two supporting observations --------------------------------------

step "2. The two observations §7 recorded next to the table"

for repo in aks129/healthclaw-guardrails aks129/healthclaw-mcp-server; do
  tags=$(tag_list ghcr.io "$repo")
  if [ -z "$tags" ]; then
    bad "ghcr.io/${repo}: tag list unreadable — the '1.10 alias' observation was NOT measured this run."
    continue
  fi
  printf '  ghcr.io/%s tags: %s\n' "$repo" "$tags"
  if [ "$tags" = "$BASELINE_TAGS" ]; then
    note "ghcr.io/${repo}: tag list identical to 2026-08-16."
  else
    note "ghcr.io/${repo}: tag list DIFFERS from 2026-08-16 ('${BASELINE_TAGS}')."
  fi
  case ", ${tags}," in
    *", 1.10,"*) note "ghcr.io/${repo}: a 1.10 alias now exists; §7 recorded none." ;;
    *)           note "ghcr.io/${repo}: still no 1.10 alias, unlike every prior minor." ;;
  esac

  latest=$(manifest_digest ghcr.io "$repo" latest)
  pinned=$(manifest_digest ghcr.io "$repo" 1.10.0)
  if [ -z "$latest" ] || [ -z "$pinned" ]; then
    bad "ghcr.io/${repo}: could not read both latest and 1.10.0 — the 'latest == 1.10.0' observation was NOT measured this run."
  elif [ "$latest" = "$pinned" ]; then
    note "ghcr.io/${repo}: latest still resolves to the same digest as 1.10.0 (${latest})."
  else
    note "ghcr.io/${repo}: latest (${latest}) has DIVERGED from 1.10.0 (${pinned}) — the drift the compose comment describes."
  fi
done

step "Result"
if [ "$fail" -eq 0 ]; then
  printf '  \033[32mno assertion failed\033[0m\n'
  exit 0
fi
printf '  \033[31mat least one assertion failed\033[0m\n'
exit 1
