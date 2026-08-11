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

# ---------------------------------------------------------------------------
step "1. The same resource, both ways"

direct=$(curl -sS -u "${AIDBOX_CLIENT}:${AIDBOX_SECRET}" \
  "${AIDBOX_URL}/fhir/Patient/pt-demo")
echo "  direct from Aidbox:"
echo "$direct" | python3 -c "import json,sys; r=json.load(sys.stdin); print('   ', json.dumps({k:r.get(k) for k in ('name','identifier','birthDate','address')})[:300])"

proxied=$(curl -sS -H "X-Tenant-Id: ${TENANT}" \
  "${HC}/r6/fhir/Patient/pt-demo")
echo "  through the guardrail proxy:"
echo "$proxied" | python3 -c "import json,sys; r=json.load(sys.stdin); print('   ', json.dumps({k:r.get(k) for k in ('name','identifier','birthDate','address','meta')})[:300])"

# The assertion is on the distinctive values, not on the shape of the mask:
# checking for '***masked***' would pass if redaction were replaced by a
# function that returned that string and nothing else.
python3 - "$direct" "$proxied" <<'PY'
import json, sys
direct, proxied = json.loads(sys.argv[1]), json.loads(sys.argv[2])
raw = json.dumps(proxied)
leaked = [tok for tok in ("Alvarez", "Maria", "MRN-88214", "221 Baker St",
                          "555-867-5309", "1974-03-11") if tok in raw]
if leaked:
    print(f"  \033[31mFAIL\033[0m identifiers survived redaction: {leaked}")
    sys.exit(1)
if "Alvarez" not in json.dumps(direct):
    print("  \033[31mFAIL\033[0m Aidbox did not return the identified record; "
          "is the seed loaded?")
    sys.exit(1)
print("  \033[32mPASS\033[0m Aidbox holds the full record; the agent's path does not")
PY
[ $? -ne 0 ] && fail=1

# ---------------------------------------------------------------------------
step "2. The read left a record"

audit=$(curl -sS -H "X-Tenant-Id: ${TENANT}" "${HC}/r6/fhir/AuditEvent?_count=1")
echo "$audit" | python3 -c "
import json,sys
b=json.load(sys.stdin)
n=b.get('total', len(b.get('entry',[])))
print(f'    AuditEvent entries: {n}')
raw=json.dumps(b)
for tok in ('Alvarez','MRN-88214','221 Baker St'):
    if tok in raw:
        print(f'  \033[31mFAIL\033[0m PHI in the audit trail: {tok}'); sys.exit(1)
if not b.get('entry'):
    print('  \033[31mFAIL\033[0m the read emitted no AuditEvent'); sys.exit(1)
print('  \033[32mPASS\033[0m audit written, and PHI-free')
"
[ $? -ne 0 ] && fail=1

# ---------------------------------------------------------------------------
step "3. A write, blocked twice"

body='{"resourceType":"Observation","status":"final",
       "subject":{"reference":"Patient/pt-demo"},
       "effectiveDateTime":"2026-08-11",
       "code":{"coding":[{"system":"http://loinc.org","code":"85354-9"}]},
       "valueQuantity":{"value":128,"unit":"mmHg"}}'

code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  -H "X-Tenant-Id: ${TENANT}" -H 'Content-Type: application/fhir+json' \
  -d "$body" "${HC}/r6/fhir/Observation")
echo "    no step-up token          -> HTTP ${code}"
[ "$code" = "401" ] && ok "refused without a step-up credential" \
                    || bad "expected 401 without a step-up token, got ${code}"

token=$(curl -sS -X POST -H 'Content-Type: application/json' \
  -H "X-Tenant-Id: ${TENANT}" -d "{\"tenant_id\":\"${TENANT}\"}" \
  "${HC}/r6/fhir/internal/step-up-token" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))")

if [ -z "$token" ]; then
  bad "could not mint a step-up token"
else
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "X-Tenant-Id: ${TENANT}" -H "X-Step-Up-Token: ${token}" \
    -H 'Content-Type: application/fhir+json' \
    -d "$body" "${HC}/r6/fhir/Observation")
  echo "    step-up, no human         -> HTTP ${code}"
  [ "$code" = "428" ] && ok "held for human confirmation" \
                      || bad "expected 428 pending confirmation, got ${code}"
fi

# ---------------------------------------------------------------------------
step "4. Grade the deployment"

curl -sS "${HC}/r6/fhir/\$conformance?format=text" | sed -n '1,4p' | sed 's/^/    /'
grade=$(curl -sS "${HC}/r6/fhir/\$conformance" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('grade',''), d['score']['passed'], d['score']['total'])" 2>/dev/null)
echo "    grade: ${grade}"
case "$grade" in
  "A 7 7") ok "Grade A, 7/7" ;;
  *)       bad "expected 'A 7 7', got '${grade}'" ;;
esac

# ---------------------------------------------------------------------------
if [ "$fail" -ne 0 ]; then
  printf '\n\033[31mWalkthrough FAILED.\033[0m A guardrail this example claims did not hold.\n'
  exit 1
fi
printf '\n\033[32mWalkthrough passed.\033[0m The agent did useful work and could not finish alone.\n'
