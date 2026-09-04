#!/usr/bin/env bash
# The connector walkthrough, against a PUBLIC FHIR server.
#
# Why this exists: `examples/aidbox-healthclaw-guardrails/scripts/walkthrough.sh`
# proves the `aidbox` kind and needs a local Aidbox, a seeded `Patient/pt-demo`
# and a Basic credential. The `hapi` and `generic` kinds were proven live on
# 2026-08-16 by a re-pointed COPY of that script living in an uncommitted
# scratch directory (evidence pack R8), so the most load-bearing measured claim
# in the process documents — "2 of 4 connector kinds proven live" — rested on a
# run nobody but its author could re-execute. This is that script, committed.
#
# One parametrized script rather than `walkthrough-hapi.sh` plus a near-copy
# for `generic`: two near-copies of an assertion list is exactly how the Aidbox
# script's status codes drifted from its own README (#499 and the "tells the
# truth" test that now pins them).
#
# It creates SYNTHETIC resources on a shared public server and does not delete
# them. Never point it at anything holding real records.
#
# Usage:
#   scripts/walkthrough-upstream.sh hapi
#   scripts/walkthrough-upstream.sh generic
#   UPSTREAM_URL=https://example.org/R4 scripts/walkthrough-upstream.sh generic
#
# It does NOT start the app. Start it first, in upstream mode, e.g.:
#   export FHIR_UPSTREAM_KIND=hapi FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4
#   export READ_AUTH_ENABLED=true STEP_UP_SECRET=dev-secret APP_ENV=development
#   export SQLALCHEMY_DATABASE_URI=sqlite:////tmp/hc-hapi.db PORT=5099
#   uv run flask --app main init-db && uv run python main.py
#
# Each step prints what it asked for and what came back. Steps that assert a
# guardrail FAIL LOUDLY when the guardrail does not hold — a walkthrough that
# prints "OK" whatever happens is a demo of itself, not of the system.
set -uo pipefail

KIND="${1:-${FHIR_UPSTREAM_KIND:-}}"
case "$KIND" in
  hapi)    DEFAULT_URL="https://hapi.fhir.org/baseR4" ;;
  generic) DEFAULT_URL="https://server.fire.ly/R4" ;;
  "")      echo "usage: $0 <hapi|generic>   (or set FHIR_UPSTREAM_KIND)" >&2; exit 2 ;;
  *)       DEFAULT_URL="" ;;
esac
UPSTREAM="${UPSTREAM_URL:-${FHIR_UPSTREAM_URL:-$DEFAULT_URL}}"
[ -z "$UPSTREAM" ] && { echo "set UPSTREAM_URL for kind '${KIND}'" >&2; exit 2; }

HC="http://localhost:${HEALTHCLAW_PORT:-${PORT:-5099}}"
TENANT="${TENANT:-desktop-demo}"
# Public sandboxes are slow and shared. Firely answered /metadata in 7.4s on
# 2026-09-04, so a 10s timeout would report a working server as unreachable.
CURL=(curl -sS --max-time 60)

# Every resource this run creates carries the same nonce, so a re-run against
# the same shared server is a DIFFERENT body. Without it hapi.fhir.org's
# duplicate-detection interceptor answers the second run with 412 HAPI-2840,
# which is what graded the deployment F on 2026-08-16 (#514).
NONCE=$(python3 -c "import uuid; print(uuid.uuid4().hex[:12])")
SYSTEM="urn:walkthrough:${NONCE}"

# Distinctive synthetic values. The redaction assertions test for THESE
# strings, not for the shape of a mask: asserting on '***' would pass if
# redaction were replaced by a function that returns '***' and nothing else.
FAMILY="Zzyzxbrook"
GIVEN="Everdeen"
MRN="900775409"
PHONE="555-0166"
BIRTHDATE="1980-03-11"

fail=0
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }
note() { printf '  \033[33mNOTE\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m%s\033[0m\n' "$*"; exit 2; }

printf 'walkthrough-upstream.sh  kind=%s  upstream=%s\n' "$KIND" "$UPSTREAM"
printf 'run nonce %s  (all created resources carry it)\n' "$NONCE"
printf 'date %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# ---------------------------------------------------------------------------
step "0. Preflight"

health=$("${CURL[@]}" "${HC}/r6/fhir/health" 2>/dev/null)
[ -z "$health" ] && die "The guardrail proxy is not answering on ${HC}.
Is it up?  PORT=5099 uv run python main.py
On macOS, port 5000 belongs to AirPlay Receiver — use PORT=5099."

python3 - "$health" "$KIND" <<'PY' || exit 2
import json, sys
h = json.loads(sys.argv[1]); want_kind = sys.argv[2]
mode = h.get("mode"); up = h.get("checks", {}).get("upstream")
print(f"    proxy version {h.get('version')}, mode {mode}")

if mode != "upstream":
    print(f"""
\033[31mThe proxy is running in LOCAL mode — it is not talking to {want_kind} at all.\033[0m
Everything below would pass against the proxy's own SQLite store and prove
nothing about the connector. Check FHIR_UPSTREAM_URL is exported.""")
    sys.exit(1)

if not isinstance(up, dict):
    # 'misconfigured' or 'not_configured' — #513. A named upstream that could
    # not be built used to report healthy while writes went to SQLite.
    print(f"\n\033[31mupstream check is '{up}', not a proxy health payload.\033[0m")
    sys.exit(1)

print(f"    upstream: kind={up.get('kind')} software={up.get('software')!r} "
      f"fhirVersion={up.get('fhir_version')} status={up.get('status')}")
if up.get("status") != "connected":
    print(f"\n\033[31mThe proxy cannot reach the upstream (status: {up.get('status')}).\033[0m")
    sys.exit(1)
if up.get("kind") != want_kind:
    print(f"\n\033[31mThe proxy reports kind={up.get('kind')!r}, not {want_kind!r}.\033[0m")
    print("One kind standing in for another proves nothing about this one.")
    sys.exit(1)
print("  \033[32mPASS\033[0m proxy is in upstream mode, connected, and reports "
      "the kind it was built as")
PY
[ $? -ne 0 ] && exit 2

# The Aidbox walkthrough ASSERTS that the upstream refuses anonymous callers,
# because there the proxy holding its own credential is the point. A public
# sandbox is auth=none by design, so the same assertion here would be a
# guaranteed red that says nothing. Reported, not asserted — and the property
# it would have proven is recorded as NOT demonstrated rather than assumed.
anon=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "${UPSTREAM}/metadata?_summary=true")
if [ "$anon" = "200" ]; then
  note "the upstream serves ANONYMOUS callers (HTTP 200). Expected for a public
       sandbox (this kind degrades to anonymous when no credential is set).
       The \"proxy holds its own credential\" property is NOT demonstrated by
       this run."
else
  ok "the upstream refuses anonymous callers (HTTP ${anon}) — the credential matters"
fi

# READ_AUTH_ENABLED is on, so a tenant header alone gets a 401, not a redacted
# record. Minted BEFORE the reads: without it every read returns an
# OperationOutcome, which contains no PHI, and every redaction assertion below
# would pass VACUOUSLY on a refusal. That was #499.
token=$("${CURL[@]}" -X POST -H 'Content-Type: application/json' \
  -H "X-Tenant-Id: ${TENANT}" -d "{\"tenant_id\":\"${TENANT}\"}" \
  "${HC}/r6/fhir/internal/step-up-token" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
[ -z "$token" ] && die "Could not mint a step-up token on ${HC}.
Every read and write below needs one. Is STEP_UP_SECRET set?"
AUTH=(-H "X-Tenant-Id: ${TENANT}" -H "X-Step-Up-Token: ${token}")

# ---------------------------------------------------------------------------
step "1a. A subject to write about (synthetic)"

patient=$(cat <<JSON
{"resourceType":"Patient",
 "name":[{"family":"${FAMILY}","given":["${GIVEN}"]}],
 "identifier":[{"system":"${SYSTEM}","value":"${MRN}"}],
 "birthDate":"${BIRTHDATE}",
 "telecom":[{"system":"phone","value":"${PHONE}"}]}
JSON
)

created=$("${CURL[@]}" -X POST "${AUTH[@]}" \
  -H 'Content-Type: application/fhir+json' -d "$patient" "${HC}/r6/fhir/Patient")
PID=$(echo "$created" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
if [ -z "$PID" ]; then
  bad "guardrailed create did not return a Patient id:"
  echo "$created" | head -c 400 | sed 's/^/        /'
  die "Nothing below would be testing this connector."
fi
ok "guardrailed create -> upstream (Patient/${PID})"

# The proxy reporting its own 201 says nothing about storage. Ask the upstream,
# going around the proxy.
landed=$("${CURL[@]}" "${UPSTREAM}/Patient?identifier=${SYSTEM}|${MRN}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "${landed:-0}" -ge 1 ] \
  && ok "the write reached the upstream (asked ${UPSTREAM} directly)" \
  || bad "the create returned an id but ${UPSTREAM} has no such Patient"

# ---------------------------------------------------------------------------
step "1b. A CLINICAL write, and two gates that do not substitute for each other"

# Four requests, not two. A sequence of two refusals only shows that SOME
# refusal happened; it cannot tell you whether the second gate would have
# accepted the first gate's credential. The matrix can.
#
#   neither          -> 428   human confirmation is missing
#   confirmed only   -> 401   a confirmation is not a credential
#   token only       -> 428   a credential is not a confirmation
#   both             -> 201   and only then
#
# The bare request reports 428 rather than 401 because the human-in-the-loop
# check runs in a before_request hook, ahead of every handler's auth gate.
#
# This matrix is a property of CLINICAL writes. The Patient create above takes
# the step-up token alone, because require_human_confirmation fires on
# CLINICAL_RESOURCE_TYPES and Consent only (evidence pack R12).
obs=$(cat <<JSON
{"resourceType":"Observation","status":"final",
 "subject":{"reference":"Patient/${PID}"},
 "effectiveDateTime":"2026-09-04",
 "identifier":[{"system":"${SYSTEM}","value":"obs-${NONCE}"}],
 "code":{"coding":[{"system":"http://loinc.org","code":"85354-9"}]},
 "valueQuantity":{"value":128,"unit":"mmHg"}}
JSON
)

write() {  # write <expected> <label> [extra curl args...]
  local expected="$1" label="$2"; shift 2
  local code
  code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST \
    -H "X-Tenant-Id: ${TENANT}" -H 'Content-Type: application/fhir+json' \
    "$@" -d "$obs" "${HC}/r6/fhir/Observation")
  printf '    %-26s -> HTTP %s\n' "$label" "$code"
  [ "$code" = "$expected" ] && ok "$label" \
                            || bad "$label: expected ${expected}, got ${code}"
}

write 428 "neither gate"
write 401 "confirmed, no credential" -H 'X-Human-Confirmed: true'
write 428 "credential, no human" -H "X-Step-Up-Token: ${token}"
write 201 "both gates satisfied" -H "X-Step-Up-Token: ${token}" \
                                 -H 'X-Human-Confirmed: true'

obs_landed=$("${CURL[@]}" "${UPSTREAM}/Observation?identifier=${SYSTEM}|obs-${NONCE}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "${obs_landed:-0}" -ge 1 ] \
  && ok "the Observation reached the upstream (${obs_landed} found)" \
  || bad "the write returned 201 but the upstream has no such Observation"

# ---------------------------------------------------------------------------
step "2. The same resource, both ways"

proxied=$("${CURL[@]}" "${AUTH[@]}" "${HC}/r6/fhir/Patient/${PID}")
echo "  through the guardrail proxy:"
echo "$proxied" | python3 -c "
import json,sys
r=json.load(sys.stdin)
keys=('resourceType','id','name','identifier','birthDate','telecom','_source')
print('   ', json.dumps({k:r.get(k) for k in keys if k in r})[:400])" 2>/dev/null

python3 - "$proxied" "$PID" "$FAMILY" "$GIVEN" "$MRN" "$PHONE" "$BIRTHDATE" <<'PY'
import json, sys
proxied = json.loads(sys.argv[1]); pid = sys.argv[2]; leaks = sys.argv[3:]

# 1. We are looking at the record, not at an error about the record. Checks 2
#    and 3 are satisfied by ANY response lacking the identifiers — including a
#    refusal — so this one comes first and gates them.
if proxied.get("resourceType") != "Patient" or proxied.get("id") != pid:
    print(f"  \033[31mFAIL\033[0m the proxy did not return Patient/{pid}. "
          "Nothing below would be testing redaction:")
    print("        " + json.dumps(proxied)[:300]); sys.exit(1)

# 2. It came from the upstream, not from the proxy's own SQLite. This single
#    field is the whole distance between this run and a false pass — it is the
#    one check that failed in the Medplum QA run against no Medplum at all
#    (evidence pack R2).
if proxied.get("_source") != "upstream":
    print(f"  \033[31mFAIL\033[0m _source is {proxied.get('_source')!r}, not "
          "'upstream'. The record came from the proxy's own store, so this "
          "proves nothing about the connector."); sys.exit(1)

# 3. The distinctive values are gone.
raw = json.dumps(proxied)
leaked = [t for t in leaks if t in raw]
if leaked:
    print(f"  \033[31mFAIL\033[0m identifiers survived redaction: {leaked}")
    sys.exit(1)
print("  \033[32mPASS\033[0m the upstream holds the full record; the agent's "
      "path does not (_source=upstream)")
PY
[ $? -ne 0 ] && fail=1

# ---------------------------------------------------------------------------
step "3. The read left a record"

"${CURL[@]}" "${AUTH[@]}" "${HC}/r6/fhir/AuditEvent?_count=5" | python3 -c "
import json,sys
b=json.load(sys.stdin)
# Same trap as step 2: a refusal is a JSON object with no entries, and 'no PHI
# in it' is trivially true of a refusal. Check the shape first.
if b.get('resourceType') != 'Bundle':
    print('  \033[31mFAIL\033[0m expected a Bundle of AuditEvents, got:')
    print('        ' + json.dumps(b)[:300]); sys.exit(1)
n=b.get('total', len(b.get('entry',[])))
print(f'    AuditEvent entries: {n}')
if not b.get('entry'):
    print('  \033[31mFAIL\033[0m the read emitted no AuditEvent'); sys.exit(1)
raw=json.dumps(b)
for tok in ('${FAMILY}','${GIVEN}','${MRN}','${PHONE}','${BIRTHDATE}'):
    if tok in raw:
        print(f'  \033[31mFAIL\033[0m PHI in the audit trail: {tok}'); sys.exit(1)
print('  \033[32mPASS\033[0m audit written, and PHI-free')
"
[ $? -ne 0 ] && fail=1

# ---------------------------------------------------------------------------
step "4. Grade the deployment"

# NOT an assertion that the grade is A. In upstream mode error fidelity fails
# for a reason worth stating rather than hiding behind a softer threshold: a
# search carrying an unknown parameter is forwarded upstream, which answers
# 404/502, instead of being refused by the guardrail with an OperationOutcome
# naming the parameter. Tracked as #498.
#
# So the assertion is: every OTHER property holds, and error fidelity is the
# only failure. Anything else regressing goes red, and the day #498 closes this
# still passes at 7/7 without an edit.
"${CURL[@]}" "${HC}/r6/fhir/\$conformance" | python3 -c "
import json, sys
d = json.load(sys.stdin)
score = d['score']
failed = [p['key'] for p in d.get('properties', []) if not p.get('passed')]
print(f\"    grade {d.get('grade')} ({score['passed']}/{score['total']})\"
      + (' [cached]' if d.get('cached') else ''))
if failed:
    print('    failing properties: ' + ', '.join(sorted(failed)))
KNOWN = {'error_fidelity'}
unexpected = set(failed) - KNOWN
if unexpected:
    print('  \033[31mFAIL\033[0m properties that should hold did not: '
          + ', '.join(sorted(unexpected)))
    sys.exit(1)
if failed:
    print('  \033[32mPASS\033[0m ' + str(score['passed']) + '/'
          + str(score['total']) + ' — only error fidelity fails (known, #498)')
else:
    print('  \033[32mPASS\033[0m Grade A, 7/7')
"
[ $? -ne 0 ] && fail=1

# ---------------------------------------------------------------------------
step "5. What the agent actually connects to"

# The MCP server runs in Docker. When Docker is down this reports HTTP 000,
# which is an ABSENCE, not a refusal — red because nothing ran, which is the
# correct report. Do not read a 000 as "the MCP server rejected the call".
MCP="http://localhost:${MCP_PORT:-3001}"
mcp_code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST "${MCP}/mcp/rpc" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>/dev/null)
echo "    tools/list, no token      -> HTTP ${mcp_code}"
if [ "$mcp_code" = "401" ]; then
  ok "the MCP server refuses unauthenticated callers"
elif [ "$mcp_code" = "000" ]; then
  bad "expected 401 from an unauthenticated tools/list, got 000 (NOT RUNNING = not checked)"
else
  bad "expected 401 from an unauthenticated tools/list, got ${mcp_code}"
fi

# ---------------------------------------------------------------------------
printf '\nCreated on %s and left in place (synthetic): Patient/%s, one Observation,\n' \
  "$UPSTREAM" "$PID"
printf 'plus whatever $conformance created. All carry nonce %s.\n' "$NONCE"

if [ "$fail" -ne 0 ]; then
  printf '\n\033[31mWalkthrough FAILED.\033[0m A guardrail this connector claims did not hold,\n'
  printf 'or a step could not be run. Read which above — they are not the same thing.\n'
  exit 1
fi
printf '\n\033[32mWalkthrough passed.\033[0m The agent did useful work and could not finish alone.\n'
