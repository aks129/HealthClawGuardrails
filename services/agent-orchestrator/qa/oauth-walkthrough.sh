#!/usr/bin/env bash
# The MCP authorization walkthrough, as something you can run against a
# deployed pair (docs/specs/2026-08-16-mcp-authorization.md §8).
#
# It asserts, it fails loudly, and each failure names the guarantee that
# broke. A walkthrough that prints PASS whatever happens is a demo of itself.
# It runs the machine-checkable chain: the demo policy (CAREAGENTS_CONSENT_URL
# unset on Flask), so /oauth/authorize answers the redirect without a person.
# The consent surface has its own suite (tests/test_careagents_consent.py)
# and the §8.4 end-user run is a person in claude.ai's own dialog, not this.
#
#   MCP_URL=https://mcp.healthclaw.io/mcp \
#   MCP_AUTH_TOKEN=<static token, optional, for R5> \
#   bash services/agent-orchestrator/qa/oauth-walkthrough.sh
#
# Data: the synthetic public demo tenant only. Nothing here can reach PHI by
# construction (§6.1), so the output is safe to paste into an issue.
set -uo pipefail

MCP_URL="${MCP_URL:-https://mcp.healthclaw.io/mcp}"
MCP_AUTH_TOKEN="${MCP_AUTH_TOKEN:-}"
REDIRECT_URI="${REDIRECT_URI:-http://localhost/callback}"
CALL_EVERY_TOOL="${CALL_EVERY_TOOL:-0}"
# The public demo tenant. A6's authorize binds it through the demo policy and
# ignores the header; R3's own FHIR-audience mint goes through the header
# policy, which defaults to a tenant that is not public and refuses (found by
# the first live run, 2026-09-06). Both name it, so both mint.
DEMO_TENANT="${DEMO_TENANT:-desktop-demo}"
#
# Running against a local pair: the MCP server requires an https://
# MCP_CANONICAL_RESOURCE and answers the enriched challenge only on that host,
# so a local run needs a TLS-terminating proxy in front of each service and
# curl's --connect-to to route the canonical hostname there (the #672 comment
# records one such setup). Nothing in the script depends on it.

fail=0; passed=0
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; passed=$((passed + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }
die()  { printf '\n\033[31m%s\033[0m\n' "$*"; exit 2; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }
need curl; need python3

# --- helpers ------------------------------------------------------------------
# Every request writes its headers and body to files; assertions read them.
hdr=$(mktemp); body=$(mktemp); trap 'rm -f "$hdr" "$body"' EXIT
req() { curl -sS -o "$body" -D "$hdr" -w '%{http_code}' "$@" 2>/dev/null; }
header() { awk -v k="$1" 'tolower($1) == tolower(k":") { sub(/^[^:]*: */, ""); sub(/\r$/, ""); print }' "$hdr" | tail -1; }
jget() { python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d = d[int(k)] if isinstance(d, list) else d.get(k)
    if d is None: break
print("" if d is None else (json.dumps(d) if isinstance(d,(dict,list)) else d))' "$body" "$1" 2>/dev/null; }
mcp() { # mcp <bearer or -> <session id or -> <json body>
  local auth="$1" sid="$2" json="$3"; shift 3
  local args=(-X POST "$MCP_URL" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' --data "$json")
  [ "$auth" != "-" ] && args+=(-H "Authorization: Bearer $auth")
  [ "$sid" != "-" ] && args+=(-H "Mcp-Session-Id: $sid")
  req "${args[@]}"
}
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"oauth-walkthrough","version":"1"}}}'

# --- A1: the refusal offers a way forward -------------------------------------
step "A1  Unauthenticated initialize is 401 with resource_metadata and scope, and no error="
code=$(mcp - - "$INIT")
www=$(header WWW-Authenticate)
[ "$code" = "401" ] && ok "401 ($MCP_URL)" || bad "expected 401, got $code: the lock does not hold"
case "$www" in *resource_metadata=*) ok "resource_metadata present";; *) bad "no resource_metadata in: ${www:-<none>} (RFC 9728)";; esac
case "$www" in *scope=*) ok "scope present";; *) bad "no scope in the challenge";; esac
case "$www" in *error=*) bad "error= on a credential-less 401 (RFC 6750 §3.1)";; *) ok "no error= without a credential";; esac
prm_url=$(printf '%s' "$www" | python3 -c 'import re,sys; m=re.search(r"resource_metadata=\"([^\"]+)\"", sys.stdin.read()); print(m.group(1) if m else "")')
[ -n "$prm_url" ] || die "Cannot continue without the resource_metadata URL from A1."

# --- A2/A3: the way forward exists and names our audience ---------------------
step "A2  The resource_metadata URL from A1 serves the document"
code=$(req "$prm_url" -H 'Origin: https://claude.ai')
[ "$code" = "200" ] && ok "200 $prm_url" || bad "$code from $prm_url"
ctype=$(header Content-Type); case "$ctype" in application/json*) ok "content-type $ctype";; *) bad "content-type $ctype";; esac
resource=$(jget resource); as0=$(jget authorization_servers.0); scopes=$(jget scopes_supported)
[ -n "$resource" ] && [ -n "$as0" ] && [ -n "$scopes" ] && ok "resource, authorization_servers, scopes_supported present" || bad "document incomplete"
acao=$(header Access-Control-Allow-Origin); [ "$acao" = "*" ] && ok "readable from a browser (ACAO *)" || bad "no Access-Control-Allow-Origin on the PRM (§9.4)"
step "A3  PRM resource string-equals the URL the client connected to"
[ "$resource" = "$MCP_URL" ] && ok "resource == $MCP_URL" || bad "resource is $resource, connected to $MCP_URL: the audience we check is not the one the client will request"

# --- A4: the authorization server is discoverable from its issuer -------------
step "A4  AS metadata resolves where a client looks, issuer matches, every URL https, S256"
as_meta=""
for loc in "$as0/.well-known/oauth-authorization-server" "$as0/.well-known/openid-configuration"; do
  code=$(req "$loc"); if [ "$code" = "200" ]; then as_meta="$loc"; break; fi
done
[ -n "$as_meta" ] && ok "found at $as_meta" || bad "no metadata at either location a client tries for issuer $as0"
issuer=$(jget issuer); [ "$issuer" = "$as0" ] && ok "issuer == authorization_servers[0]" || bad "issuer $issuer != $as0"
for k in authorization_endpoint token_endpoint registration_endpoint; do
  v=$(jget "$k"); case "$v" in https://*) ok "$k is https";; *) bad "$k is ${v:-missing} (OAuth 2.1 requires https)";; esac
done
case "$(jget code_challenge_methods_supported)" in *S256*) ok "S256";; *) bad "no S256";; esac
case "$(jget authorization_response_iss_parameter_supported)" in True|true) ok "RFC 9207 iss advertised";; *) bad "iss not advertised";; esac
AUTHZ=$(jget authorization_endpoint); TOKEN=$(jget token_endpoint); REG=$(jget registration_endpoint)
[ -n "$AUTHZ" ] && [ -n "$TOKEN" ] && [ -n "$REG" ] || die "Cannot continue without the three endpoints."

# --- A5: DCR --------------------------------------------------------------------
step "A5  Dynamic client registration answers 201 with a client_id"
code=$(req -X POST "$REG" -H 'Content-Type: application/json' --data "{\"client_name\":\"oauth-walkthrough\",\"redirect_uris\":[\"$REDIRECT_URI\"],\"token_endpoint_auth_method\":\"none\"}")
client_id=$(jget client_id)
[ "$code" = "201" ] && [ -n "$client_id" ] && ok "201 client_id=$client_id" || bad "registration: $code $(cat "$body")"
[ -n "$client_id" ] || die "Cannot continue without a client."

# --- A6: authorize + token, the way a browser would --------------------------
pkce() { python3 -c 'import base64,hashlib,secrets
v=secrets.token_urlsafe(48); c=base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
print(v); print(c)'; }
authorize_for() { # authorize_for <resource> -> prints "code|state|iss|verifier" or "error:<err>"
  local res="$1"; local state="st-$RANDOM"; local pk; pk=$(pkce); local verifier challenge
  verifier=$(printf '%s' "$pk" | sed -n 1p); challenge=$(printf '%s' "$pk" | sed -n 2p)
  local code; code=$(req -G "$AUTHZ" -H "X-Tenant-Id: $DEMO_TENANT" --data-urlencode "client_id=$client_id" --data-urlencode "redirect_uri=$REDIRECT_URI" \
    --data-urlencode "code_challenge=$challenge" --data-urlencode "code_challenge_method=S256" \
    --data-urlencode "scope=fhir.read context.read" --data-urlencode "state=$state" --data-urlencode "resource=$res")
  local loc; loc=$(header Location)
  [ "$code" = "302" ] || { printf 'error:http %s %s' "$code" "$(head -c 200 "$body")"; return; }
  python3 - "$loc" "$state" "$verifier" <<'PY'
import sys
from urllib.parse import urlsplit, parse_qs
loc, state, verifier = sys.argv[1:]
q = {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}
if "error" in q: print("error:" + q["error"]); sys.exit()
if q.get("state") != state: print("error:state mismatch"); sys.exit()
print("|".join([q.get("code", ""), q.get("state", ""), q.get("iss", ""), verifier]))
PY
}
exchange() { # exchange <code> <verifier> <resource>
  req -X POST "$TOKEN" --data-urlencode "grant_type=authorization_code" --data-urlencode "code=$1" \
    --data-urlencode "code_verifier=$2" --data-urlencode "client_id=$client_id" --data-urlencode "redirect_uri=$REDIRECT_URI" --data-urlencode "resource=$3"
}
step "A6  authorize is a 302 with code, state and iss; the token endpoint answers a Bearer"
out=$(authorize_for "$MCP_URL")
case "$out" in error:*) bad "authorize: ${out#error:}"; die "Cannot continue without a code (with CAREAGENTS_CONSENT_URL set on Flask this needs a person; unset it for the machine chain).";; esac
IFS='|' read -r acode astate aiss averifier <<<"$out"
[ -n "$acode" ] && ok "code received from the Location header" || bad "no code in the redirect"
[ "$aiss" = "$issuer" ] && ok "iss == issuer" || bad "iss is '$aiss'"
code=$(exchange "$acode" "$averifier" "$MCP_URL")
mcp_token=$(jget access_token)
[ "$code" = "200" ] && [ -n "$mcp_token" ] && ok "access token received" || bad "token endpoint: $code $(head -c 200 "$body")"
[ "$(jget token_type)" = "Bearer" ] && ok "token_type Bearer" || bad "token_type $(jget token_type)"
[ -n "$(jget refresh_token)" ] && ok "refresh_token issued (§13.5)" || printf '  \033[33mNOTE\033[0m no refresh_token (pre-§13.5 server)\n'
[ -n "$mcp_token" ] || die "Cannot continue without a token."

# --- A7: the token opens the door ----------------------------------------------
step "A7  initialize with the token is 200 with a session; tools/list answers"
code=$(mcp "$mcp_token" - "$INIT"); sid=$(header Mcp-Session-Id)
if [ "$code" = "200" ] && [ -n "$sid" ]; then ok "200, Mcp-Session-Id issued"; else bad "initialize with the OAuth token: $code (is MCP_OAUTH_ENABLED=true on the server?)"; fi
code=$(mcp "$mcp_token" "$sid" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
ntools=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["result"]["tools"]))' "$body" 2>/dev/null || echo 0)
[ "$code" = "200" ] && [ "$ntools" -gt 0 ] && ok "tools/list: $ntools tools on this transport" || bad "tools/list: $code"
priv=$(python3 -c 'import json,sys; print(",".join(sorted(t["name"] for t in json.load(open(sys.argv[1]))["result"]["tools"] if t["name"] in ("fhir_get_token","fhir_seed"))))' "$body" 2>/dev/null)
[ -z "$priv" ] && ok "privileged tools withheld from this transport" || bad "privileged tools exposed: $priv"

# --- A8: every tool answers (read tier; writes are refused by design) ----------
step "A8  Read-tier tools answer through the consented tenant; write-tier tools refuse"
call() { mcp "$mcp_token" "$sid" "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}"; }
code=$(call fhir_read '{"resource_type":"Patient","resource_id":"p-1"}')
iserr=$(jget result.isError); txt=$(jget result.content.0.text)
if [ "$code" = "200" ] && [ "$iserr" != "True" ] && [ "$iserr" != "true" ]; then ok "fhir_read answered"; else
  case "$txt" in *read_credential_unavailable*) bad "fhir_read: the server could not mint a read credential (INTERNAL_TOKEN_MINT_SECRET on the server, or the mint's scope argument missing on Flask)";; *"not found"*|*404*) ok "fhir_read answered (the demo tenant has no p-1; the call reached Flask)";; *) bad "fhir_read: $code ${txt:0:160}";; esac
fi
code=$(call fhir_commit_write '{"resource":{"resourceType":"Observation","status":"final"},"operation":"create","_stepUpToken":"forged"}')
case "$(jget result.content.0.text)" in *read-only*) ok "fhir_commit_write refused: a consent to read is not a consent to write";; *) bad "fhir_commit_write was not refused as read-only: $(jget result.content.0.text | head -c 200)";; esac
if [ "$CALL_EVERY_TOOL" = "1" ]; then
  mcp "$mcp_token" "$sid" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' >/dev/null
  python3 -c 'import json,sys
for t in json.load(open(sys.argv[1]))["result"]["tools"]:
    print(t["name"], "1" if (t.get("annotations") or {}).get("readOnlyHint") else "0")' "$body" > "$hdr.tools"
  called=0; answered=0; skipped=0
  while read -r name ro; do
    [ "$ro" = "1" ] || continue
    case "$name" in
      fhir_read) args='{"resource_type":"Patient","resource_id":"p-1"}';;
      fhir_search) args='{"resource_type":"Patient","params":{"_count":"1"}}';;
      *) skipped=$((skipped+1)); continue;;
    esac
    called=$((called+1)); c=$(call "$name" "$args"); [ "$c" = "200" ] && answered=$((answered+1))
  done < "$hdr.tools"; rm -f "$hdr.tools"
  printf '  read-tier tools called %d, answered %d, skipped %d (no fixture in this script)\n' "$called" "$answered" "$skipped"
fi

# --- The refusal chain (§8.2) ---------------------------------------------------
step "R1  No credential: 401"
code=$(mcp - - "$INIT"); [ "$code" = "401" ] && ok "401" || bad "$code"
step "R2  A random string as bearer: 401 invalid_token"
code=$(mcp "not-a-token-$RANDOM" - "$INIT"); www=$(header WWW-Authenticate)
[ "$code" = "401" ] && ok "401" || bad "$code"
case "$www" in *invalid_token*) ok "error=invalid_token";; *) bad "no invalid_token in: $www";; esac
step "R3  A token minted at the same issuer for the FHIR audience, replayed here: 401 invalid_token (never 403)"
fhir_res="${issuer}/r6/fhir"
out=$(authorize_for "$fhir_res")
case "$out" in error:*) bad "could not mint a FHIR-audience token: ${out#error:}";;
*) IFS='|' read -r c2 s2 i2 v2 <<<"$out"; exchange "$c2" "$v2" "$fhir_res" >/dev/null; fhir_token=$(jget access_token)
   if [ -n "$fhir_token" ]; then code=$(mcp "$fhir_token" - "$INIT"); www=$(header WWW-Authenticate)
     [ "$code" = "401" ] && ok "401 (audience validation is real)" || bad "$code: a token for another audience was $( [ "$code" = 200 ] && echo ACCEPTED || echo 'answered with something other than 401')"
     case "$www" in *invalid_token*) ok "invalid_token";; *) bad "challenge lacks invalid_token: $www";; esac
   else bad "no FHIR-audience token to replay"; fi;;
esac
step "R4  Expiry is enforced at the resource"
printf '  \033[33mNOTE\033[0m needs a token older than OAUTH_TOKEN_TTL; covered by src/oauth-path.test.ts and the introspection suite, not repeated here\n'
step "R5  MCP_AUTH_TOKEN still returns the full tool list"
if [ -n "$MCP_AUTH_TOKEN" ]; then
  code=$(mcp "$MCP_AUTH_TOKEN" - "$INIT"); sid2=$(header Mcp-Session-Id)
  code=$(mcp "$MCP_AUTH_TOKEN" "$sid2" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
  [ "$code" = "200" ] && ok "static token: tools/list 200" || bad "static token: $code (the lock was replaced, not added to)"
else printf '  \033[33mNOTE\033[0m MCP_AUTH_TOKEN not provided; skipped\n'; fi
step "R6  Boot without MCP_AUTH_TOKEN refuses to start"
printf '  \033[33mNOTE\033[0m a boot property; asserted in src/oauth-path.test.ts (R6) and src/step-up-gate.test.ts\n'
step "R7  MCP_OAUTH_ENABLED=false refuses an otherwise valid token"
printf '  \033[33mNOTE\033[0m needs a second deployment with the flag off; asserted in src/oauth-path.test.ts (R7)\n'
step "R8  Credentials in tool arguments reach Flask in no form"
code=$(call fhir_read '{"resource_type":"Patient","resource_id":"p-1","_tenantId":"victim-tenant","_stepUpToken":"forged","_authorization":"Bearer forged"}')
txt=$(jget result.content.0.text)
case "$txt" in *victim-tenant*|*forged*) bad "a tool-argument credential is echoed back: ${txt:0:160}";; *) ok "no argument credential surfaces in the answer (Flask-side audit rows are the second half: agent_id oauth:<client>, tenant desktop-demo)";; esac

# --- §9.4 browser readability -----------------------------------------------------
step "CORS  The preflight and the 401 are readable from a browser context"
code=$(req -X OPTIONS "$MCP_URL" -H 'Origin: https://claude.ai' -H 'Access-Control-Request-Method: POST')
[ "$code" = "204" ] && [ "$(header Access-Control-Allow-Origin)" = "*" ] && ok "OPTIONS 204 with ACAO *" || bad "preflight: $code ACAO '$(header Access-Control-Allow-Origin)'"
code=$(mcp - - "$INIT")
[ "$(header Access-Control-Allow-Origin)" = "*" ] && ok "401 carries ACAO *" || bad "401 without ACAO"
case "$(header Access-Control-Expose-Headers)" in *WWW-Authenticate*) ok "WWW-Authenticate exposed";; *) bad "WWW-Authenticate not exposed";; esac

printf '\n%s: %d assertions passed%s\n' "$( [ $fail -eq 0 ] && echo PASS || echo FAIL )" "$passed" "$( [ $fail -eq 0 ] && echo '' || echo ', at least one guarantee did not hold' )"
exit $fail
