#!/usr/bin/env bash
# The five-step walkthrough from the article, as something you can run.
#
# Each step prints what it asked for and what came back. Steps that assert a
# guardrail FAIL LOUDLY when the guardrail does not hold — a walkthrough that
# prints "OK" whatever happens is a demo of itself, not of the system.
set -uo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

AIDBOX_URL="${AIDBOX_URL:-http://localhost:8080}"
AIDBOX_CLIENT="${AIDBOX_CLIENT:-root}"
AIDBOX_SECRET="${AIDBOX_SECRET:-qNbQS6sw82}"
HC="http://localhost:${HEALTHCLAW_PORT:-5000}"
TENANT="${TENANT:-demo}"

fail=0
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }
die()  { printf '\n\033[31m%s\033[0m\n' "$*"; exit 2; }

# ---------------------------------------------------------------------------
# Preflight. Two setup failures produce misleading symptoms further down, so
# they are named here rather than left to surface as a puzzling 401 in step 1.
step "0. Preflight"

health=$(curl -sS "${HC}/r6/fhir/health" 2>/dev/null)
[ -z "$health" ] && die "The guardrail proxy is not answering on ${HC}.
Is it up?  docker compose ps
On macOS, port 5000 belongs to AirPlay Receiver — set HEALTHCLAW_PORT=5099."

python3 - "$health" <<'PY' || exit 2
import json, sys
h = json.loads(sys.argv[1])
mode = h.get("mode")
up = h.get("checks", {}).get("upstream")
print(f"    proxy version {h.get('version')}, mode {mode}")

if mode != "upstream":
    print("""
\033[31mThe proxy is running in LOCAL mode — it is not talking to Aidbox at all.\033[0m
Everything below would pass against the proxy's own SQLite store and prove
nothing about Aidbox. Check FHIR_UPSTREAM_URL reached the container:
    docker compose exec healthclaw printenv FHIR_UPSTREAM_URL""")
    sys.exit(1)

status = up.get("status") if isinstance(up, dict) else up
if status != "connected":
    print(f"""
\033[31mThe proxy cannot reach Aidbox (upstream status: {status}).\033[0m
The two causes, in the order they actually happen:

  1. Aidbox is not activated. It answers EVERY route with a 302 to
     "Log in to activate Aidbox" — including /health, so this looks like a
     network fault rather than a licence one. Open http://localhost:8080 and
     click "Continue with Aidbox account", or set BOX_LICENSE in .env.

  2. The proxy image predates upstream authentication. Images published
     before v1.10.0 ignore FHIR_UPSTREAM_CLIENT_ID and
     FHIR_UPSTREAM_CLIENT_SECRET entirely, so the proxy calls Aidbox
     anonymously and the AccessPolicy refuses it. Pull again, or build from
     a checkout:
       docker compose -f docker-compose.yaml -f docker-compose.build.yaml up -d --build""")
    sys.exit(1)
print("  \033[32mPASS\033[0m proxy is in upstream mode and connected to Aidbox")
PY

# The example claims the proxy needs its own credential. That claim is only
# worth anything if Aidbox refuses callers who lack one — and Aidbox runs here
# with BOX_SECURITY_DEV_MODE on, which is exactly the setting that could make
# the AccessPolicy vacuous. Assert it rather than assume it.
anon=$(curl -sS -o /dev/null -w '%{http_code}' "${AIDBOX_URL}/fhir/Patient/pt-demo")
if [ "$anon" = "200" ]; then
  bad "Aidbox served Patient/pt-demo to an ANONYMOUS caller (HTTP 200).
       The AccessPolicy is not constraining anything, so 'the proxy holds its
       own credential' is not demonstrated here. Check BOX_SECURITY_DEV_MODE."
else
  ok "Aidbox refuses anonymous callers (HTTP ${anon}) — the credential matters"
fi

# ---------------------------------------------------------------------------
# READS are authenticated too, because READ_AUTH_ENABLED is on. A tenant
# header alone gets a 401, not a redacted record — the same token that
# authorises a write is what proves the tenant claim on a read.
#
# This is minted here, before step 1, rather than in step 3 where it used to
# live. Without it every read below returns an OperationOutcome, and an
# OperationOutcome contains no PHI: the redaction assertions passed
# VACUOUSLY, on a refusal rather than on a record. That is why step 1 now
# checks it received a Patient before it checks what the Patient contains.
token=$(curl -sS -X POST -H 'Content-Type: application/json' \
  -H "X-Tenant-Id: ${TENANT}" -d "{\"tenant_id\":\"${TENANT}\"}" \
  "${HC}/r6/fhir/internal/step-up-token" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))")
[ -z "$token" ] && die "Could not mint a step-up token on ${HC}.
Every read and write below needs one. Is STEP_UP_SECRET set in .env?"
READ_AUTH=(-H "X-Tenant-Id: ${TENANT}" -H "X-Step-Up-Token: ${token}")

# ---------------------------------------------------------------------------
step "1. The same resource, both ways"

direct=$(curl -sS -u "${AIDBOX_CLIENT}:${AIDBOX_SECRET}" \
  "${AIDBOX_URL}/fhir/Patient/pt-demo")
echo "  direct from Aidbox:"
echo "$direct" | python3 -c "import json,sys; r=json.load(sys.stdin); print('   ', json.dumps({k:r.get(k) for k in ('name','identifier','birthDate','address')})[:300])"

proxied=$(curl -sS "${READ_AUTH[@]}" "${HC}/r6/fhir/Patient/pt-demo")
echo "  through the guardrail proxy:"
echo "$proxied" | python3 -c "import json,sys; r=json.load(sys.stdin); print('   ', json.dumps({k:r.get(k) for k in ('resourceType','id','name','identifier','birthDate','address')})[:300])"

# Three assertions, in this order. The first exists because the other two
# are satisfied by ANY response that lacks the identifiers — including a
# refusal, which is what this step was actually receiving.
python3 - "$direct" "$proxied" <<'PY'
import json, sys
direct, proxied = json.loads(sys.argv[1]), json.loads(sys.argv[2])

# 1. We are looking at the record, not at an error about the record.
if proxied.get("resourceType") != "Patient" or proxied.get("id") != "pt-demo":
    print("  \033[31mFAIL\033[0m the proxy did not return Patient/pt-demo. "
          "Nothing below would be testing redaction:")
    print("        " + json.dumps(proxied)[:300])
    sys.exit(1)

# 2. Aidbox really does hold the identified record, so the comparison means
#    something.
if "Alvarez" not in json.dumps(direct):
    print("  \033[31mFAIL\033[0m Aidbox did not return the identified record; "
          "is the seed loaded?")
    sys.exit(1)

# 3. The distinctive values are gone. Asserted on the values themselves, not
#    on the shape of the mask: checking for '***masked***' would pass if
#    redaction were replaced by a function returning that string and nothing
#    else.
raw = json.dumps(proxied)
leaked = [tok for tok in ("Alvarez", "Maria", "MRN-88214", "221 Baker St",
                          "555-867-5309", "1974-03-11") if tok in raw]
if leaked:
    print(f"  \033[31mFAIL\033[0m identifiers survived redaction: {leaked}")
    sys.exit(1)
print("  \033[32mPASS\033[0m Aidbox holds the full record; the agent's path does not")
PY
[ $? -ne 0 ] && fail=1

# ---------------------------------------------------------------------------
step "2. The read left a record"

audit=$(curl -sS "${READ_AUTH[@]}" "${HC}/r6/fhir/AuditEvent?_count=5")
echo "$audit" | python3 -c "
import json,sys
b=json.load(sys.stdin)
# Same trap as step 1: a refusal is a JSON object with no entries, and 'no
# PHI in it' is trivially true of a refusal. Check the shape first.
if b.get('resourceType') != 'Bundle':
    print('  \033[31mFAIL\033[0m expected a Bundle of AuditEvents, got:')
    print('        ' + json.dumps(b)[:300]); sys.exit(1)
n=b.get('total', len(b.get('entry',[])))
print(f'    AuditEvent entries: {n}')
if not b.get('entry'):
    print('  \033[31mFAIL\033[0m the read emitted no AuditEvent'); sys.exit(1)
raw=json.dumps(b)
for tok in ('Alvarez','MRN-88214','221 Baker St','555-867-5309'):
    if tok in raw:
        print(f'  \033[31mFAIL\033[0m PHI in the audit trail: {tok}'); sys.exit(1)
print('  \033[32mPASS\033[0m audit written, and PHI-free')
"
[ $? -ne 0 ] && fail=1

# ---------------------------------------------------------------------------
step "3. A write, and two gates that do not substitute for each other"

# Four requests, not two. A sequence of two refusals only shows that SOME
# refusal happened; it cannot tell you whether the second gate would have
# accepted the first gate's credential. The matrix can: each gate is
# presented on its own, and each one refuses on its own.
#
#   neither          -> 428   human confirmation is missing
#   confirmed only   -> 401   a confirmation is not a credential
#   token only       -> 428   a credential is not a confirmation
#   both             -> 201   and only then
#
# The bare request reports 428 rather than 401 because the human-in-the-loop
# check runs in a before_request hook, ahead of every handler's auth gate.
# That ordering is deliberate — it is what stops an unauthenticated caller
# reaching the handler — so a bare write reports the human gate, not the
# credential one. An earlier version of this script asserted the reverse.
#
# tests/test_aidbox_example_tells_the_truth.py replays this exact matrix
# against the app in CI, so these four numbers cannot drift from the server.
body='{"resourceType":"Observation","status":"final",
       "subject":{"reference":"Patient/pt-demo"},
       "effectiveDateTime":"2026-08-11",
       "code":{"coding":[{"system":"http://loinc.org","code":"85354-9"}]},
       "valueQuantity":{"value":128,"unit":"mmHg"}}'

write() {  # write <expected> <label> [extra curl args...]
  local expected="$1" label="$2"; shift 2
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "X-Tenant-Id: ${TENANT}" -H 'Content-Type: application/fhir+json' \
    "$@" -d "$body" "${HC}/r6/fhir/Observation")
  printf '    %-26s -> HTTP %s\n' "$label" "$code"
  [ "$code" = "$expected" ] && ok "$label" \
                            || bad "$label: expected ${expected}, got ${code}"
}

# $token was minted before step 1 — reads need it too.
write 428 "neither gate"
write 401 "confirmed, no credential" -H 'X-Human-Confirmed: true'
write 428 "credential, no human" -H "X-Step-Up-Token: ${token}"
write 201 "both gates satisfied" -H "X-Step-Up-Token: ${token}" \
                                 -H 'X-Human-Confirmed: true'

# The write is only real if Aidbox holds it. Ask Aidbox, going around the
# proxy — the proxy reporting its own 201 says nothing about storage.
landed=$(curl -sS -u "${AIDBOX_CLIENT}:${AIDBOX_SECRET}" \
  "${AIDBOX_URL}/fhir/Observation?subject=Patient/pt-demo&code=85354-9" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "${landed:-0}" -ge 1 ] && ok "the Observation reached Aidbox (${landed} found)" \
                         || bad "the write returned 201 but Aidbox has no such Observation"

# ---------------------------------------------------------------------------
step "4. Grade the deployment"

curl -sS "${HC}/r6/fhir/\$conformance?format=text" | sed -n '1,2p' | sed 's/^/    /'

# NOT an assertion that the grade is A. In upstream mode it is B, and the one
# property that fails — error fidelity — fails for a reason worth stating
# rather than hiding behind a softer threshold: a search carrying an unknown
# parameter is forwarded to Aidbox, which answers 404/502, instead of being
# refused by the guardrail with an OperationOutcome naming the parameter.
# That is a real gap in proxy mode, tracked as #498.
#
# So the assertion is: every OTHER property holds, and error fidelity is the
# only failure. Anything else regressing goes red, and the day the gap is
# closed this still passes at 7/7 without an edit.
curl -sS "${HC}/r6/fhir/\$conformance" | python3 -c "
import json, sys
d = json.load(sys.stdin)
score = d['score']
failed = [p['key'] for p in d.get('properties', []) if not p.get('passed')]
print(f\"    grade {d.get('grade')} ({score['passed']}/{score['total']})\")
KNOWN = {'error_fidelity'}
unexpected = set(failed) - KNOWN
if unexpected:
    print('  \033[31mFAIL\033[0m properties that should hold did not: '
          + ', '.join(sorted(unexpected)))
    sys.exit(1)
if failed:
    print('  \033[32mPASS\033[0m ' + str(score['passed']) + '/'
          + str(score['total']) + ' — the only failure is error fidelity,')
    print('       which in upstream mode measures how Aidbox answers an '
          'unknown search')
    print('       parameter, not how the guardrail does. Known, and stated '
          'rather than')
    print('       graded away.')
else:
    print('  \033[32mPASS\033[0m Grade A, 7/7')
"
[ $? -ne 0 ] && fail=1

# ---------------------------------------------------------------------------
if [ "$fail" -ne 0 ]; then
  printf '\n\033[31mWalkthrough FAILED.\033[0m A guardrail this example claims did not hold.\n'
  exit 1
fi
printf '\n\033[32mWalkthrough passed.\033[0m The agent did useful work and could not finish alone.\n'
