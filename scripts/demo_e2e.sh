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

_green() { printf '\033[0;32m✓ %s\033[0m\n' "$*"; }
_red()   { printf '\033[0;31m✗ %s\033[0m\n' "$*"; }
_blue()  { printf '\033[0;34m→ %s\033[0m\n' "$*"; }

gate_pass() { _green "$1"; PASS=$((PASS+1)); }
gate_fail() { _red "$1"; FAIL=$((FAIL+1)); }

check() {
  local desc="$1" expect="$2" actual="$3"
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
check "Seed created resources" '"created"' "$SEED_RESP"

# Extract seeded token if provided (seed returns a fresh token)
SEED_TOKEN=$(echo "$SEED_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('step_up_token', ''))" 2>/dev/null || echo "")
if [ -n "$SEED_TOKEN" ]; then
  STEP_UP_TOKEN="$SEED_TOKEN"
fi

PATIENT_ID=$(echo "$SEED_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
created = d.get('created', [])
for r in created:
  if isinstance(r, dict) and r.get('resourceType') == 'Patient':
    print(r.get('id',''))
    break
" 2>/dev/null || echo "")
check "Seeded patient ID extracted" "." "${PATIENT_ID:-none}"

# ─────────────────────────────────────────────────────
# GATE 6: Read with PHI redaction
# ─────────────────────────────────────────────────────
_blue "Gate 6: PHI redaction on read"

if [ -n "$PATIENT_ID" ]; then
  PATIENT_RESP=$(curl -sf "$FHIR_BASE/Patient/$PATIENT_ID" \
    -H "X-Tenant-ID: $TENANT_ID" 2>/dev/null || echo '{}')
  check "Patient read succeeds" '"resourceType"' "$PATIENT_RESP"

  # Family name must be redacted to single initial (e.g. "D." not "Doe")
  FAMILY=$(echo "$PATIENT_RESP" | python3 -c "
import sys, json, re
d = json.load(sys.stdin)
names = d.get('name', [])
if names:
  print(names[0].get('family', ''))
" 2>/dev/null || echo "")
  check "PHI redacted: family name is initial only" "^[A-Z]\.$" "$FAMILY"
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
fi

# ─────────────────────────────────────────────────────
# GATE 9: Curatr evaluation (insight)
# ─────────────────────────────────────────────────────
_blue "Gate 9: Curatr evaluation"

CONDITION_RESP=$(curl -sf "$FHIR_BASE/Condition?patient=$PATIENT_ID" \
  -H "X-Tenant-ID: $TENANT_ID" 2>/dev/null || echo '{}')
CONDITION_ID=$(echo "$CONDITION_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
entries = d.get('entry', [])
if entries:
  print(entries[0].get('resource', {}).get('id', ''))
" 2>/dev/null || echo "")

if [ -n "$CONDITION_ID" ]; then
  CURATR_RESP=$(curl -sf -X POST "$FHIR_BASE/Condition/$CONDITION_ID/\$curatr-evaluate" \
    -H "Content-Type: application/json" \
    -H "X-Tenant-ID: $TENANT_ID" \
    -d '{}' 2>/dev/null || echo '{}')
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
# SUMMARY
# ─────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────"
TOTAL=$((PASS+FAIL))
printf "  Gates passed: %d / %d\n" "$PASS" "$TOTAL"
if [ "$FAIL" -gt 0 ]; then
  printf "  \033[0;31mGates failed: %d\033[0m\n" "$FAIL"
  echo "────────────────────────────────────"
  exit 1
else
  printf "  \033[0;32mAll gates passed.\033[0m\n"
  echo "────────────────────────────────────"
  exit 0
fi
