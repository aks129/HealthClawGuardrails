#!/usr/bin/env bash
# demo_e2e.sh — End-to-end smoke test: ingest → curate → insight → approve → act
#
# Tests the full guardrail stack in one command.
# Requires Flask (:5000) and MCP server (:3001) to be running.
# Usage:
#   ./scripts/demo_e2e.sh                     # use defaults
#   TENANT_ID=my-tenant ./scripts/demo_e2e.sh # custom tenant
#   FHIR_BASE=http://localhost:5000/r6/fhir ./scripts/demo_e2e.sh
#
# Exit codes: 0 = all gates passed, 1 = gate failure

set -euo pipefail

FHIR_BASE="${FHIR_BASE:-http://localhost:5000/r6/fhir}"
MCP_BASE="${MCP_BASE:-http://localhost:3001}"
TENANT_ID="${TENANT_ID:-demo-e2e-$(date +%s)}"
STEP_UP_SECRET="${STEP_UP_SECRET:-dev-secret-change-in-production}"
PASS=0
FAIL=0
# Every gate that runs, whether it passes or fails, marks itself ran. A gate
# whose body is skipped (an empty variable, a short-circuited `if`) never
# calls this and the final ran-count catches it — PASS+FAIL alone cannot,
# because a silently-skipped gate contributes to neither.
declare -a GATES_RAN=()
gate_ran() { GATES_RAN+=("$1"); }

_green() { printf '\033[0;32m✓ %s\033[0m\n' "$*"; }
_red()   { printf '\033[0;31m✗ %s\033[0m\n' "$*"; }
_blue()  { printf '\033[0;34m→ %s\033[0m\n' "$*"; }

gate_pass() { _green "$1"; PASS=$((PASS+1)); }
gate_fail() { _red "$1"; FAIL=$((FAIL+1)); }

check() {
  local desc="$1" expect="$2" actual="$3"
  gate_ran "$desc"
  if echo "$actual" | grep -q "$expect" 2>/dev/null; then
    gate_pass "$desc"
  else
    gate_fail "$desc (expected '$expect' in: ${actual:0:120})"
  fi
}

mcp_call() {
  # Call an MCP tool via the HTTP bridge
  local tool="$1" args="$2"
  curl -sf -X POST "$MCP_BASE/mcp/rpc" \
    -H "Content-Type: application/json" \
    -H "X-Tenant-ID: $TENANT_ID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

echo ""
_blue "HealthClaw Guardrails — End-to-End Gate Test"
_blue "Tenant: $TENANT_ID | FHIR: $FHIR_BASE | MCP: $MCP_BASE"
echo ""

# ─────────────────────────────────────────────────────
# GATE 1: Liveness
# ─────────────────────────────────────────────────────
_blue "Gate 1: Liveness"

HEALTH=$(curl -sf "$FHIR_BASE/health" 2>/dev/null || echo "FAIL")
check "Flask health endpoint responds" '"status"' "$HEALTH"

MCP_HEALTH=$(curl -sf "$MCP_BASE/health" 2>/dev/null || echo "FAIL")
check "MCP server health endpoint responds" '"ok"\|"healthy"\|200' "$MCP_HEALTH" || true
# MCP health check is informational — not a blocking gate

# ─────────────────────────────────────────────────────
# GATE 2: Tenant isolation — write blocked without header
# ─────────────────────────────────────────────────────
_blue "Gate 2: Tenant isolation"

NO_TENANT=$(curl -sf -o /dev/null -w "%{http_code}" -X POST "$FHIR_BASE/Patient" \
  -H "Content-Type: application/json" \
  -d '{"resourceType":"Patient"}' 2>/dev/null || echo "000")
check "Write without X-Tenant-ID returns 4xx" "^4" "$NO_TENANT"

# ─────────────────────────────────────────────────────
# GATE 3: Write authorization — step-up required
# ─────────────────────────────────────────────────────
_blue "Gate 3: Write authorization"

NO_TOKEN=$(curl -sf -o /dev/null -w "%{http_code}" -X POST "$FHIR_BASE/Patient" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{"resourceType":"Patient","name":[{"family":"Test"}]}' 2>/dev/null || echo "000")
check "Clinical POST without step-up token returns 401" "401" "$NO_TOKEN"

# ─────────────────────────────────────────────────────
# GATE 4: Get step-up token
# ─────────────────────────────────────────────────────
_blue "Gate 4: Step-up token issuance"

TOKEN_RESP=$(curl -sf -X POST "$FHIR_BASE/internal/step-up-token" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{}' 2>/dev/null || echo '{}')
STEP_UP_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")
check "Step-up token issued" "." "$STEP_UP_TOKEN"

# ─────────────────────────────────────────────────────
# GATE 5: Seed demo data
# ─────────────────────────────────────────────────────
_blue "Gate 5: Data seeding (ingest)"

SEED_RESP=$(curl -sf -X POST "$FHIR_BASE/internal/seed" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -H "X-Step-Up-Token: $STEP_UP_TOKEN" \
  -d "{\"tenant_id\":\"$TENANT_ID\"}" 2>/dev/null || echo '{}')
check "Seed created resources" '"created_count"' "$SEED_RESP"

# Extract seeded token if provided (seed returns a fresh token)
SEED_TOKEN=$(echo "$SEED_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('step_up_token', ''))" 2>/dev/null || echo "")
if [ -n "$SEED_TOKEN" ]; then
  STEP_UP_TOKEN="$SEED_TOKEN"
fi

PATIENT_ID=$(curl -sf "$FHIR_BASE/Patient?_count=1" \
  -H "X-Tenant-ID: $TENANT_ID" 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
entries = d.get('entry', [])
print(entries[0].get('resource', {}).get('id', '') if entries else '')
" 2>/dev/null || echo "")
# Sentinel starts with '!' so an empty id can never satisfy the pattern.
check "Seeded patient ID extracted" "^[A-Za-z0-9][A-Za-z0-9._-]*$" "${PATIENT_ID:-!none}"

# ─────────────────────────────────────────────────────
# GATE 6: Read with PHI redaction
# ─────────────────────────────────────────────────────
_blue "Gate 6: PHI redaction on read"

if [ -n "$PATIENT_ID" ]; then
  PATIENT_RESP=$(curl -sf "$FHIR_BASE/Patient/$PATIENT_ID" \
    -H "X-Tenant-ID: $TENANT_ID" 2>/dev/null || echo '{}')
  check "Patient read succeeds" '"resourceType"' "$PATIENT_RESP"

  # Positive assertion first (the seed's raw PII must not survive redaction
  # anywhere in the body — catches a leak in any field, not just the one
  # below), then the specific format each redacted field must take. A single
  # narrow assertion (just the family initial) passed once while the given
  # name on the same record leaked in full — this checks every field
  # r6/redaction.py's own docstring claims to touch.
  RAW_LEAK_COUNT=$(echo "$PATIENT_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
body = json.dumps(d)
raw = ['Rivera', 'Maria', 'Elena', '1985-03-15', '617-555-0198']
print(sum(1 for v in raw if v in body))
" 2>/dev/null || echo "?")
  check "PHI redacted: none of the seeded raw values appear in the response" "^0$" "$RAW_LEAK_COUNT"

  REDACTED_FIELDS=$(echo "$PATIENT_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
names = d.get('name', [{}])
name = names[0] if names else {}
family = name.get('family', '')
given = (name.get('given') or [''])[0]
birth = d.get('birthDate', '')
telecoms = d.get('telecom', [])
phone = next((t.get('value', '') for t in telecoms if t.get('system') == 'phone'), '')
print('|'.join([family, given, birth, phone]))
" 2>/dev/null || echo "|||")
  FAMILY=$(echo "$REDACTED_FIELDS" | cut -d'|' -f1)
  GIVEN=$(echo "$REDACTED_FIELDS" | cut -d'|' -f2)
  BIRTH=$(echo "$REDACTED_FIELDS" | cut -d'|' -f3)
  PHONE=$(echo "$REDACTED_FIELDS" | cut -d'|' -f4)
  check "PHI redacted: family name is initial only" "^[A-Z]\.$" "$FAMILY"
  check "PHI redacted: given name is initial only" "^[A-Z]\.$" "$GIVEN"
  check "PHI redacted: birth date is year only" "^1985$" "$BIRTH"
  check "PHI redacted: telecom value is [Redacted]" "^\[Redacted\]$" "$PHONE"
fi

# ─────────────────────────────────────────────────────
# GATE 7: Audit trail written
# ─────────────────────────────────────────────────────
_blue "Gate 7: Audit trail"

AUDIT_RESP=$(curl -sf "$FHIR_BASE/AuditEvent?_count=5" \
  -H "X-Tenant-ID: $TENANT_ID" 2>/dev/null || echo '{}')
AUDIT_COUNT=$(echo "$AUDIT_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(len(d.get('entry', [])))
" 2>/dev/null || echo "0")
check "AuditEvents recorded (count ≥ 1)" "[1-9]" "$AUDIT_COUNT"

# ─────────────────────────────────────────────────────
# GATE 8: Tenant isolation — cross-tenant read blocked
# ─────────────────────────────────────────────────────
_blue "Gate 8: Cross-tenant isolation"

if [ -n "$PATIENT_ID" ]; then
  OTHER_TENANT="other-tenant-$(date +%s)"
  CROSS_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "$FHIR_BASE/Patient/$PATIENT_ID" \
    -H "X-Tenant-ID: $OTHER_TENANT" 2>/dev/null || echo "000")
  check "Cross-tenant read returns 404 (not 200)" "404" "$CROSS_STATUS"

  # The direct-read shape and the search shape are different code paths (a
  # dropped filter_by(tenant_id=...) on one does not necessarily drop it on
  # the other) — search under the other tenant and confirm this patient's id
  # is absent from the result set, not just that a targeted read 404s.
  CROSS_SEARCH=$(curl -s "$FHIR_BASE/Patient?_count=50" \
    -H "X-Tenant-ID: $OTHER_TENANT" 2>/dev/null || echo '{}')
  CROSS_SEARCH_HIT=$(echo "$CROSS_SEARCH" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ids = [e.get('resource', {}).get('id', '') for e in d.get('entry', [])]
print('LEAKED' if '$PATIENT_ID' in ids else 'absent')
" 2>/dev/null || echo "?")
  check "Cross-tenant search does not return this patient" "^absent$" "$CROSS_SEARCH_HIT"
fi

# ─────────────────────────────────────────────────────
# GATE 9: Curatr evaluation (insight)
# ─────────────────────────────────────────────────────
_blue "Gate 9: Curatr evaluation"

# The `patient` search param matches subject.reference, so it needs the
# full "Patient/{id}" form — a bare id matches nothing.
CONDITION_RESP=$(curl -sf "$FHIR_BASE/Condition?patient=Patient/$PATIENT_ID" \
  -H "X-Tenant-ID: $TENANT_ID" 2>/dev/null || echo '{}')
CONDITION_ID=$(echo "$CONDITION_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
entries = d.get('entry', [])
if entries:
  print(entries[0].get('resource', {}).get('id', ''))
" 2>/dev/null || echo "")

if [ -n "$CONDITION_ID" ]; then
  CURATR_RESP=$(curl -sf "$FHIR_BASE/Condition/$CONDITION_ID/\$curatr-evaluate" \
    -H "X-Tenant-ID: $TENANT_ID" 2>/dev/null || echo '{}')
  check "Curatr evaluation returns result" '"issues"\|"quality_score"\|"resourceType"' "$CURATR_RESP"
else
  _blue "  (skip — no Condition found in seeded data)"
fi

# ─────────────────────────────────────────────────────
# GATE 10: Human-in-the-loop (approve → act)
# ─────────────────────────────────────────────────────
_blue "Gate 10: Human-in-the-loop enforcement"

# Refresh step-up token
TOKEN_RESP2=$(curl -sf -X POST "$FHIR_BASE/internal/step-up-token" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{}' 2>/dev/null || echo '{}')
STEP_UP_TOKEN2=$(echo "$TOKEN_RESP2" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")

# Clinical POST with valid step-up token but WITHOUT X-Human-Confirmed must return 428
HITL_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" -X POST "$FHIR_BASE/Condition" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -H "X-Step-Up-Token: $STEP_UP_TOKEN2" \
  -d "{\"resourceType\":\"Condition\",\"subject\":{\"reference\":\"Patient/$PATIENT_ID\"},\"clinicalStatus\":{\"coding\":[{\"system\":\"http://terminology.hl7.org/CodeSystem/condition-clinical\",\"code\":\"active\"}]},\"verificationStatus\":{\"coding\":[{\"system\":\"http://terminology.hl7.org/CodeSystem/condition-ver-status\",\"code\":\"confirmed\"}]},\"code\":{\"text\":\"Test\"}}" \
  2>/dev/null || echo "000")
check "Clinical write without X-Human-Confirmed returns 428" "428" "$HITL_STATUS"

# ─────────────────────────────────────────────────────
# GATE 11: Action propose / commit in simulation mode
# ─────────────────────────────────────────────────────
_blue "Gate 11: Action propose/commit (simulation mode)"

# Derive the Flask app root from FHIR_BASE (strip /r6/fhir suffix)
APP_BASE="${FHIR_BASE%/r6/fhir}"

# Refresh step-up token for action commit
TOKEN_RESP3=$(curl -s -X POST "$FHIR_BASE/internal/step-up-token" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" \
  -d '{}' 2>/dev/null || echo '{}')
STEP_UP_TOKEN3=$(echo "$TOKEN_RESP3" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")

# Propose a phone-call action
PROPOSE_RESP=$(curl -s -X POST "$APP_BASE/r6/actions/propose" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT_ID" \
  -d '{"kind":"phone-call","payload":{"to":"Demo Pharmacy","phone":"617-555-0100","body":"Demo refill call script"}}' \
  2>/dev/null || echo '{}')
ACTION_ID=$(echo "$PROPOSE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
check "Action proposed — id returned" "^[0-9a-f-]\{36\}$" "${ACTION_ID:-none}"

if [ -n "$ACTION_ID" ]; then
  # Commit submits the action; the human gate holds it at awaiting_confirmation
  # until an out-of-band approval claims it (proposed -> awaiting_confirmation
  # -> executing). See STATUS_TRANSITIONS in r6/actions/models.py.
  COMMIT_RESP=$(curl -s -X POST "$APP_BASE/r6/actions/$ACTION_ID/commit" \
    -H "Content-Type: application/json" \
    -H "X-Tenant-Id: $TENANT_ID" \
    -H "X-Step-Up-Token: $STEP_UP_TOKEN3" \
    -H "X-Human-Confirmed: true" \
    2>/dev/null || echo '{}')
  COMMIT_STATUS=$(echo "$COMMIT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
  check "Action commit parks at the human gate" "awaiting_confirmation" "$COMMIT_STATUS"

  # Verify audit trail recorded ProposedAction events (propose + commit = ≥ 2)
  AUDIT_ACTIONS=$(curl -s "$FHIR_BASE/AuditEvent?_count=50" \
    -H "X-Tenant-ID: $TENANT_ID" 2>/dev/null || echo '{}')
  AUDIT_ACTION_COUNT=$(echo "$AUDIT_ACTIONS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
entries = d.get('entry', [])
count = sum(1 for e in entries if 'ProposedAction' in json.dumps(e))
print(count)
" 2>/dev/null || echo "0")
  check "Audit trail contains ≥ 2 ProposedAction entries" "[2-9]\|[1-9][0-9]" "$AUDIT_ACTION_COUNT"
fi

# ─────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────
#
# Every named check this script can perform on a fully healthy stack. A gate
# whose body is skipped entirely (an upstream extraction came back empty, a
# short-circuited `if`) contributes to neither PASS nor FAIL — so "N of N,
# all passed" can be true with N silently smaller than this script actually
# has to say. Compare the ran-count against this fixed total, not against
# itself, and name what never ran.
readonly EXPECTED_CHECKS=(
  "Flask health endpoint responds"
  "MCP server health endpoint responds"
  "Write without X-Tenant-ID returns 4xx"
  "Clinical POST without step-up token returns 401"
  "Step-up token issued"
  "Seed created resources"
  "Seeded patient ID extracted"
  "Patient read succeeds"
  "PHI redacted: none of the seeded raw values appear in the response"
  "PHI redacted: family name is initial only"
  "PHI redacted: given name is initial only"
  "PHI redacted: birth date is year only"
  "PHI redacted: telecom value is [Redacted]"
  "AuditEvents recorded (count ≥ 1)"
  "Cross-tenant read returns 404 (not 200)"
  "Cross-tenant search does not return this patient"
  "Curatr evaluation returns result"
  "Clinical write without X-Human-Confirmed returns 428"
  "Action proposed — id returned"
  "Action commit parks at the human gate"
  "Audit trail contains ≥ 2 ProposedAction entries"
)

echo ""
echo "────────────────────────────────────"
TOTAL=$((PASS+FAIL))
printf "  Gates passed: %d / %d\n" "$PASS" "$TOTAL"

MISSING=()
for name in "${EXPECTED_CHECKS[@]}"; do
  found=0
  for ran in "${GATES_RAN[@]}"; do
    [ "$ran" = "$name" ] && found=1 && break
  done
  [ "$found" -eq 0 ] && MISSING+=("$name")
done

if [ "${#MISSING[@]}" -gt 0 ]; then
  _red "Gates that never ran (silently skipped, not failed): ${#MISSING[@]}"
  for name in "${MISSING[@]}"; do
    printf "    - %s\n" "$name"
  done
fi

if [ "$FAIL" -gt 0 ] || [ "${#MISSING[@]}" -gt 0 ]; then
  [ "$FAIL" -gt 0 ] && printf "  \033[0;31mGates failed: %d\033[0m\n" "$FAIL"
  echo "────────────────────────────────────"
  exit 1
else
  printf "  \033[0;32mAll gates passed, all %d checks ran.\033[0m\n" "${#EXPECTED_CHECKS[@]}"
  echo "────────────────────────────────────"
  exit 0
fi
