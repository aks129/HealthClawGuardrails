# MCP authorization — making the locked endpoint reachable without unlocking it

**Status: proposed. Design only. No code in this branch.**

| | |
|---|---|
| Closes | phase 1 closes [#523](https://github.com/aks129/HealthClawGuardrails/issues/523) |
| Also open | [#290](https://github.com/aks129/HealthClawGuardrails/issues/290) closes on the §8.4 end-user run. [#568](https://github.com/aks129/HealthClawGuardrails/issues/568) tracks what remains after it |
| Canonical resource | `https://mcp.healthclaw.io/mcp` (council ruling D4, `docs/2026-09-02-council-ruling.md`) |
| Issuer | `https://app.healthclaw.io` (same ruling) |
| Feature set | 6 — Surfaces (`docs/prd/06-surfaces.md` §6 names this as the missing spec) |
| Pipeline step | 3, architecture review (`docs/2026-08-16-delivery-process.md`) |
| Author | owner-surfaces |
| Decision required from | the founder, on §7 phase 3 (deploy authorization). §3.1 is settled by D4 |

Everything asserted about live behaviour in this document was executed on
2026-08-16/17 against the running deployments. The raw responses are quoted
inline. Nothing here was inferred from reading code alone, and where a claim
is an inference it says so.

---

## 1. The problem, measured

The production MCP endpoint is correctly locked and unusable by the clients it
exists for. Both halves are measured.

**The lock works.** Unauthenticated `POST /mcp`, executed:

```
HTTP/2 401
www-authenticate: Bearer
{"error":"Unauthorized"}
```

**There is no way forward.** Every document a conformant client looks for
after that 401, executed against the same host:

```
/.well-known/oauth-protected-resource       -> 404
/.well-known/oauth-protected-resource/mcp   -> 404
/.well-known/oauth-authorization-server     -> 404
/.well-known/oauth-authorization-server/mcp -> 404
/.well-known/openid-configuration           -> 404
```

The `WWW-Authenticate` header carries `Bearer` and nothing else — no
`resource_metadata`, no `scope`. A client that follows the specification has
exhausted its options at that point and falls back to asking the user for an
OAuth Client ID that does not exist. That is the loop the design partner
reported in #290.

The failure is worse than a refusal because it is a refusal that lies about
its own recoverability. A `401` is defined as the beginning of an
authorization flow. Ours is the end of one.

### 1.1 What is already correct and stays

- `MCP_AUTH_TOKEN` is required at boot when `NODE_ENV=production` and the demo
  flag is unset (`services/agent-orchestrator/src/index.ts`,
  `assertMCPAuthConfigured`). The server fail-closes without it. That once
  crash-looped production, which is the right failure.
- The credential comparison is constant-time (`crypto.timingSafeEqual`).
- The demo endpoint is a separate deployment, hard-pinned to a synthetic
  tenant, and is not touched by this design.
- Expired sessions return `404` so clients re-initialize (#490).

**Production MCP stays token-locked.** This design adds a second credential
type. It removes nothing. At no point in §7 does an unauthenticated call
succeed.

---

## 2. Prior research, verified rather than trusted

The note carried in `docs/prd/06-surfaces.md` §6 is dated 2026-08-05. Checked
against the specifications on 2026-08-16:

| Claim in the note | Verdict |
|---|---|
| RFC 9728 `/.well-known/oauth-protected-resource` + audience-validated tokens is the smallest fix keeping the existing issuer | **Holds.** MCP servers **MUST** implement RFC 9728; clients **MUST** use it for authorization-server discovery. |
| "MCP spec 2025-11-25 is stable; the 2026-07-28 RC…" | **Stale.** 2026-07-28 is now **Current**, not a release candidate. 2025-11-25 is a past revision. |
| the RC deprecates Sampling/Roots/Logging and makes protocol core stateless | Out of scope here, but note the consequence below. |

Read both revisions. The authorization mechanism is **unchanged** between
2025-11-25 and 2026-07-28 in every respect this design depends on: RFC 9728 is
MUST, the `resource_metadata` challenge parameter is the primary discovery
mechanism, the `scope` challenge parameter is SHOULD, RFC 8707 `resource` is a
client MUST, and audience validation is a server MUST. Designing to
2026-07-28 therefore costs nothing and is what this document does.

Three things did move, and two of them change the design:

1. **Dynamic Client Registration is deprecated** in 2026-07-28, "retained for
   backwards compatibility with authorization servers that do not support
   Client ID Metadata Documents." We are exactly that authorization server.
   DCR remains spec-legal, clients fall back to it when
   `client_id_metadata_document_supported` is absent from AS metadata, and
   `r6/oauth.py` already implements it. **We use DCR and do not implement
   CIMD** (§6).
2. **RFC 9207 issuer identification is new.** Authorization servers SHOULD
   return `iss` in the authorization response and advertise
   `authorization_response_iss_parameter_supported: true`. Clients MUST
   validate it when present. This is cheap to emit and is included in §3.4.
3. Protected resources **SHOULD NOT** advertise `offline_access` in
   `scopes_supported` or in the `WWW-Authenticate` scope. Noted; our
   `scopes_supported` is read-only anyway.

---

## 3. The design

### 3.1 The canonical resource URI — an owner decision, not an implementation detail

RFC 8707 and RFC 9728 both key on one string: the canonical URI of this MCP
server. It appears in four places that must agree exactly — the PRM
`resource` field, the `resource` parameter the client sends to the
authorization server, the `aud` recorded on the issued token, and the URL a
partner pastes into their connector. A mismatch in any one of them is a
rejection that reads to the partner as our bug.

It also becomes a compatibility surface the moment a token is issued:
changing it invalidates the audience of every outstanding token and every
stored client configuration.

Two candidates, both measured:

| Candidate | Measured today |
|---|---|
| `https://mcp-server-production-5112.up.railway.app/mcp` | serves the locked server; `/health` returns `healthclaw-guardrails` v1.9.0 |
| `https://mcp.healthclaw.io/mcp` | resolves (DoH: `216.150.16.129`, `216.150.1.193`) but points at **Vercel** and returns `DEPLOYMENT_NOT_FOUND` |

Recommendation: **`https://mcp.healthclaw.io/mcp`**, with the DNS repointed to
the Railway service *before* any of §7 phase 1 ships. A platform-generated
hostname baked into a token audience and a partner's saved config is a
migration we would have to run later under worse conditions. The dangling
record is separately a risk and is logged in §9.6.

This was the founder's call and it blocked phase 1. Council ruling D4 settled
it: `https://mcp.healthclaw.io/mcp`. The chosen value is pinned in one place,
`MCP_CANONICAL_RESOURCE`.

**The flag rule, replacing the fail-closed sentence this paragraph used to
carry (amendment P1-b).** The old rule was that the server refuses to start
with OAuth acceptance enabled and the variable unset. That rule cannot work
for phase 1, because phase 1 serves the metadata before OAuth acceptance
exists to be enabled. There is nothing for the boot check to key on. The rule
is now:

- **Unset.** The two well-known routes return 404 and the challenge stays a
  bare `Bearer`. That is today's behaviour, unchanged, and it is what merges
  before DNS moves.
- **Set.** Both routes serve the document and the challenge carries
  `resource_metadata` and `scope`.
- **Set, but malformed.** The server refuses to start. A value that cannot be
  an audience would otherwise turn phase 1 off in silence, which looks the same
  from outside as a deploy that worked. The rule is stated as the property, not
  as a list: the configured value must already be, character for character, the
  identifier a client derives by removing the well-known segment from the URL
  we advertise — `origin + pathname`, or `origin` alone when the path is root.
  "Absolute and `https:`" is necessary and not sufficient. Mixed case in scheme
  or host, an explicit `:443`, a `?query`, a `#fragment`, `user:pass@` userinfo,
  a bare trailing slash and dot segments all parse as https URLs, and each one
  boots a server whose challenge points at a document the challenge's own reader
  refuses (RFC 9728 §3.3 — the §9.3 mode). Userinfo does one thing more: the
  configured string is served verbatim as `resource`, so the credential lands
  in an unauthenticated, CORS-open document. The boot error names the variable
  and the shape and never the value, for that reason.
- **Host gate.** The document and the enriched challenge are served only when
  the request `Host` names the canonical host. The comparison is on hostname:
  the port, the case, and any userinfo in the header are not part of it, and no
  spelling of the header changes what is advertised, which always comes from the
  constant. `X-Forwarded-Host` is not read. The Railway hostname keeps `/health`
  and the static-token path exactly as they are.

The host gate is not defence in depth, it is conformance. RFC 9728 §3.3 has a
client reject a document whose `resource` is not the identifier it inserted
into the well-known path. Serving our document on the platform hostname would
hand that client a rejection where it currently gets a 404, and the rejection
reads to a partner as our bug.

Throughout this document `<RESOURCE>` stands for the chosen value.

### 3.2 The metadata document, and at what path

Served by the MCP server itself, unauthenticated, at **both** paths — the
specification requires clients to try the sub-path form first and the root
form second, and requires servers to implement one; serving both costs one
route and removes a whole class of client disagreement:

- `/.well-known/oauth-protected-resource/mcp` (RFC 9728 path-insertion for a
  resource whose path is `/mcp`)
- `/.well-known/oauth-protected-resource` (root fallback)

Both return the same document:

```json
{
  "resource": "<RESOURCE>",
  "authorization_servers": ["https://app.healthclaw.io"],
  "scopes_supported": ["fhir.read", "context.read"],
  "bearer_methods_supported": ["header"],
  "resource_name": "HealthClaw Guardrails",
  "resource_documentation": "https://app.healthclaw.io/r6/fhir/docs/privacy-policy"
}
```

**Serving the same bytes at the root path is not conformant, and we do it
anyway (amendment P1-d).** Under RFC 9728 §3.3, root-form insertion implies
the resource identifier `https://mcp.healthclaw.io`. Our document says
`https://mcp.healthclaw.io/mcp`. A client that fetched the root form and
checked would be right to reject it. MCP 2026-07-28 lists the root form as the
fallback to try second, so a client that reaches it has already missed the
sub-path form, and a 404 there helps nobody. We keep serving both. The
consequence for the test plan is §8.1: A2 and A3 assert against the sub-path
URL the challenge points at, never the root.

Notes on each field, because each one has a way to be wrong:

- `resource` is REQUIRED by RFC 9728 and must string-equal `<RESOURCE>`. It is
  not derived from `request.host` — a proxy that rewrites Host would then
  silently mint a different identity. It is read from the pinned constant.
  **The same rule binds the `resource_metadata` URL in the 401 (amendment
  P1-a).** That URL is built from `MCP_CANONICAL_RESOURCE` and never from
  `req.hostname`. The reason is not that a proxy is known to rewrite Host.
  Railway preserves it: production answers with
  `fullUrl: http://app.healthclaw.io/r6/fhir/Condition/…`, built from
  `request.host_url` (`r6/routes.py:1128`), and that is the public custom
  domain. Measured 2026-09-03 against `GET /r6/fhir/Condition?_count=1`.
  The reason is RFC 9728 §3.3. A client rejects a document whose `resource` is
  not the identifier it inserted into the well-known path. What we advertise
  therefore comes from the constant, whatever the request carries.
  Building a URL from the request is not a hypothetical cost. The same
  measurement shows every production authorization-server URL as `http://`,
  because `request.host_url` loses the scheme behind the proxy (§3.3, #567).
- `authorization_servers` is what makes the document useful; RFC 9728 marks it
  OPTIONAL but the MCP specification requires at least one entry. Its value is
  an **issuer identifier**, and §3.3 exists entirely because the issuer we
  have today does not resolve from it.
- `scopes_supported` is the minimal set for basic functionality, per the
  specification's scope-minimization guidance. Read scopes only. Writes are
  not an OAuth scope question here (§3.6).
- `bearer_methods_supported: ["header"]` states what we already enforce.

**These two paths sit outside the lock, and that does not weaken it.** They
are matched by neither `isMCPTransportPath` branch (`/mcp`, `/sse`,
`/messages`), so no change to the auth middleware's predicate is needed. The
document contains no secret, identifies no tenant, and grants nothing. Its
entire content is already public knowledge or is a URL. Refusing to serve it
is what produces the dead end.

They also need CORS that the transport endpoint does not have — see §9.4.

### 3.3 The authorization server must become discoverable from its own issuer

This is the part the prior research did not cover and the part most likely to
be got wrong, because the metadata document exists and returns `200`, at a URL
no conformant client will ever request.

Measured. For an issuer with a path component, `https://app.healthclaw.io/r6/fhir`,
the specification requires clients to try exactly three locations in order:

```
/.well-known/oauth-authorization-server/r6/fhir  -> 404
/.well-known/openid-configuration/r6/fhir        -> 404
/r6/fhir/.well-known/openid-configuration        -> 400
```

The document actually lives at a fourth location, which is **not** in the
client's list:

```
/r6/fhir/.well-known/oauth-authorization-server  -> 200
```

And its contents, fetched live, fail validation twice over:

```json
{"issuer":"http://app.healthclaw.io",
 "authorization_endpoint":"http://app.healthclaw.io/r6/fhir/oauth/authorize",
 "token_endpoint":"http://app.healthclaw.io/r6/fhir/oauth/token",
 "registration_endpoint":"http://app.healthclaw.io/r6/fhir/oauth/register", ...}
```

- Every URL is `http://`. OAuth 2.1 §1.5, which the MCP specification
  incorporates by reference, requires all authorization server endpoints to be
  served over HTTPS. A conformant client rejects this document on sight.
- `issuer` is `http://app.healthclaw.io` while the document is served under
  `/r6/fhir/`. Whatever we put in `authorization_servers`, the `issuer` inside
  the returned document must string-equal it, or the client rejects the
  document for issuer mismatch.

The cause of the scheme is not an OAuth bug: `oauth_discovery` builds every
URL from `request.host_url`, and behind Railway's TLS-terminating proxy that
yields `http`. Re-measured on 2026-09-03 and filed as #567: the proxy loses
the scheme, and preserves Host. This is the same class of trap CLAUDE.md already records about
Railway and ports.

**Three required changes, all in `r6/oauth.py` and its config:**

1. **Set `OAUTH_ISSUER=https://app.healthclaw.io`** and derive every endpoint
   URL in `oauth_discovery` and `smart_configuration` from it instead of
   `request.host_url`, falling back to `request.host_url` only when
   `OAUTH_ISSUER` is unset. The variable already exists and is already read;
   it is simply empty. This is preferred over installing Werkzeug's `ProxyFix`
   because `ProxyFix` changes URL generation for every route in the app, and a
   scheme change is not worth an app-wide behaviour change made in the same
   week as a webinar. `ProxyFix` remains the better long-term fix and is a
   separate item.
2. **Serve the metadata at the host root**, `GET /.well-known/oauth-authorization-server`,
   so that the issuer `https://app.healthclaw.io` (no path component) resolves
   at the first location a client tries. Keep the existing `/r6/fhir/…`
   location serving the same document so nothing that works today stops
   working.
3. **Set `authorization_servers: ["https://app.healthclaw.io"]`** in the PRM,
   matching that issuer exactly.

Choosing the path-less issuer is deliberate: it reduces the client's search
from three locations to two, and both of those are then served. The
alternative — issuer `https://app.healthclaw.io/r6/fhir` served via
path-insertion — is equally correct and strictly more moving parts.

Compatibility note: changing `issuer` from `http://app.healthclaw.io` to
`https://app.healthclaw.io` will break any existing client that pinned the
`http` value. None is known. The SMART surface is unaffected because SMART
clients read `/r6/fhir/.well-known/smart-configuration`, which has no `issuer`
field.

### 3.4 What changes in the 401

Two cases, and conflating them is how #290's own "option 1" sketch got it
wrong. RFC 6750 §3.1 says a server SHOULD NOT include `error` when no
credential was presented, because there is no token to call invalid. This
document said "does not permit", which overstates the requirement; the
behaviour it asks for is unchanged (amendment P1-c).

`<RESOURCE_ROOT>` below is the origin of `MCP_CANONICAL_RESOURCE`, and the
whole URL is built from that constant. Never from `req.hostname` — see §3.2,
amendment P1-a.

**No credential presented** — the ordinary first contact:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="<RESOURCE_ROOT>/.well-known/oauth-protected-resource/mcp",
                         scope="fhir.read context.read"
Content-Type: application/json

{"error":"Unauthorized"}
```

**A credential was presented and rejected** — wrong static token, expired
OAuth token, wrong audience, random string:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer error="invalid_token",
                         error_description="The access token is invalid, expired, or was not issued for this resource.",
                         resource_metadata="<RESOURCE_ROOT>/.well-known/oauth-protected-resource/mcp",
                         scope="fhir.read context.read"
Content-Type: application/json

{"error":"Unauthorized"}
```

What did **not** change: the status code, the body, and who gets in. The body
stays exactly `{"error":"Unauthorized"}` so that no new information is
introduced on the unauthenticated path. `error_description` deliberately does
not distinguish "expired" from "wrong audience" from "not a token at all" —
that distinction is an oracle for a caller who has no business getting one,
and the client's recovery is identical in all three cases.

`error_description` MUST NOT ever contain the presented credential, any part
of it, or a tenant identifier.

The **only** behavioural change in this section is that the refusal now says
where to go. A caller who ignores the header is in exactly the position they
are in today.

### 3.5 Audience validation: what we validate, against what, and what happens when it fails

Today the authorization server issues opaque tokens — `secrets.token_urlsafe(48)`,
stored in Redis with `{client_id, scopes, tenant_id, exp}` (`r6/oauth.py`).
There is no `aud`, no JWT, and no introspection endpoint. The `resource`
parameter is not read at `/oauth/authorize` or `/oauth/token`; it is silently
discarded. So "audience-validated tokens" is not a configuration change. It is
the five additions below, and they are small.

**At the authorization server (`r6/oauth.py`):**

1. `authorize` and `token` read the RFC 8707 `resource` parameter and record it
   on the stored token record as `aud`.
2. `token` rejects a `resource` that is not in a configured allowlist of known
   resource identifiers, with `{"error":"invalid_target"}` per RFC 8707 §2.2.
   The allowlist is explicit config, not a pattern match. An unrecognised
   audience must never be recorded as-is — that turns the audience field into
   a caller-controlled string and the check into theatre.
   **Both endpoints reject, not just `token` (amendment P2-c).** Neither
   endpoint reads `resource` today — `r6/oauth.py` contains no read of it, so
   there is nothing to reject and nothing to record. The `resource`
   at `/oauth/token` MUST equal the one recorded on the code at
   `/oauth/authorize`. It may be absent, and then it inherits that value. A
   `resource` at the token endpoint that names a different target is
   `invalid_target`, because a code issued for one audience must not be
   redeemed for another.
   **The allowlist is a map, not a set (amendment P2-b).** Each entry maps a
   resource identifier to a tenant policy, and §3.5.1 says what that means.
3. A new client-authenticated introspection endpoint,
   `POST /r6/fhir/oauth/introspect` (RFC 7662), returning
   `{active, aud, scope, tenant_id, exp, client_id}`. On any doubt it returns
   `{"active": false}`.
   **Protected, per RFC 7662 §2.1, and the credential is named now (amendment
   P2-e).** There is no client authentication anywhere in `r6/oauth.py` today:
   `token` never checks a `client_secret`, so "client-authenticated" is a new
   mechanism and not a reuse of one.
   `MCP_INTROSPECTION_CLIENT_ID` and `MCP_INTROSPECTION_CLIENT_SECRET`,
   a pre-registered confidential client, compared with `hmac.compare_digest`.
   An unprotected introspection endpoint is an oracle: it turns any captured
   token into a lookup of the tenant and scopes behind it. The MCP server is
   the only caller, and it is the only client that needs this credential.
4. `authorize` returns `iss` in the redirect (RFC 9207) and the metadata
   advertises `authorization_response_iss_parameter_supported: true`.
   **The redirect is a redirect (amendment P2-a).** `/oauth/authorize` today
   builds the redirect URL and then returns it in a JSON body
   (`r6/oauth.py:307-313`: `{"redirect": …, "code": …, "state": …}`), with no
   `iss`. OAuth 2.1 §4.1.2 requires a `302` with
   `Location: <redirect_uri>?code=…&state=…&iss=…`, and RFC 9207 §2 puts `iss`
   in that same query. A browser cannot complete a flow that ends in JSON, so
   no hosted connector can either. §8.1's A6 follows the `Location` header.
5. **Registration, per RFC 7591 (amendment P2-d).** Four changes at
   `/oauth/register`, all of them things a conformant client already expects:
   include `client_secret_expires_at` in the response (absent today,
   `r6/oauth.py:237-244`); honour `token_endpoint_auth_method: none` and treat
   that client as public (the request's value is ignored and the response is
   always `client_secret_post`, `r6/oauth.py:243`, while discovery advertises
   both methods at `r6/oauth.py:186`); reject any `redirect_uri` that is
   neither a `localhost` URL nor `https://` (stored unvalidated today,
   `r6/oauth.py:225`); and require `client_id` in the `/oauth/token` request
   for public clients, per RFC 6749 §4.1.3 — today it is checked only when the
   client sends it (`r6/oauth.py:343`). A public client sends no secret, so
   `client_id` is the only thing binding the code to the client it was issued
   to.

#### 3.5.1 Which tenant a browser-initiated authorize binds (amendment P2-b)

The tenant is config, not a header. Today it is the header:
`requested_tenant = request.headers.get('X-Tenant-Id', 'default')`
(`r6/oauth.py:284`), and that value is what the code and then the token carry.
When the `resource` parameter string-equals `MCP_CANONICAL_RESOURCE`, the
tenant is `MCP_OAUTH_DEMO_TENANT`, and `X-Tenant-Id` on that request is
ignored.

`MCP_OAUTH_DEMO_TENANT` MUST be listed in `PUBLIC_TENANTS`. That is what keeps
§6.1's boundary true: the strongest token this flow can mint is one bound to a
synthetic tenant. A browser flow has no trusted place to put a tenant header —
whoever controls the page controls it — so taking the tenant from the request
is the same defect as §9.8, arriving by a new road.

Pre-deploy check for phase 2, run against the environment rather than the
source: `APP_ENV=production` (`r6/runtime_config.py:49`),
`READ_AUTH_ENABLED=true` (`r6/runtime_config.py:105`), and
`MCP_OAUTH_DEMO_TENANT` present in `PUBLIC_TENANTS`
(`r6/command_center/access.py:95`). If the demo tenant is not
public, `authorize` refuses at `r6/oauth.py:286` and the flow fails closed,
which is the correct direction and a confusing one to debug.

**At the MCP server:**

A bearer credential that is not `MCP_AUTH_TOKEN` is treated as a candidate
OAuth access token and introspected. It is accepted only if **all** of:

- `active === true`, and
- `aud` string-equals `MCP_CANONICAL_RESOURCE`, and
- `scope` intersects the read scopes in `scopes_supported`, and
- `exp` is in the future.

Any failure returns the `invalid_token` 401 of §3.4. **Not 403.** An audience
mismatch is not insufficient scope; it is a token that was never for us, and
403 would tell the caller their token is recognised here. That distinction is
the difference between a boundary and a hint.

Constant-time comparison for the audience string is unnecessary (it is not a
secret) but the token itself is never logged, never written to an audit
`detail`, and is cached only under its SHA-256, for a TTL strictly shorter
than the token's own.

**Introspection is a hard dependency, and it fails closed.** If Flask is
unreachable, every OAuth-credentialed call returns 401. The static-token path
does not touch Flask and is unaffected. Fail-closed is the correct direction
and it is stated here so that nobody later reads the outage as a regression.

### 3.6 Three rules that fall out of audience validation, and are easy to miss

**The MCP server must stop forwarding the client's `Authorization` header when
the credential is an MCP-audience token.** Today `extractHeaders` forwards
`authorization` downstream to Flask whenever it is not the static MCP
credential. Under this design that would hand Flask a token whose audience is
`<RESOURCE>`, and Flask's `_oauth_authorizes` (`r6/read_auth.py:35-41`) checks
`tenant_id` and scope but **not** audience — so it would accept it. That is
precisely the token passthrough the MCP specification forbids, and we would
have built it by leaving code alone.

Required: on the OAuth path the MCP server drops the inbound `Authorization`
header, resolves the tenant from the introspection response, and sets
`X-Tenant-Id` itself. For the scope this design delivers (a public demo
tenant, §6) that is sufficient, because `authorize_tenant_read` returns public
tenants without a credential. The moment a protected tenant is in play, the
MCP server needs its own downstream credential, and that is a named follow-on
(§5, §6).

**`extractHeaders` is not the only way in (amendment P3-a).** The paragraph
above stops the HTTP header. The JSON-RPC tool arguments `_tenantId`,
`_stepUpToken` and `_authorization` set the same three downstream headers, in
the `CallToolRequestSchema` handler in
`services/agent-orchestrator/src/index.ts`, and a rule written against
`extractHeaders` alone leaves them open. Stopping one of two
doors is the defect shape `docs/2026-08-02-retro.md` is about.

Required: on the OAuth path those three arguments are discarded, exactly as
`isPublicDemo()` already discards them when it pins the demo tenant. The
tenant comes from introspection and from nowhere else. §8.2 asserts this as
R8, because a credential arriving in a tool argument is invisible to every
test that inspects headers.

**An OAuth scope is not a step-up token and never becomes one.** A token
carrying `fhir.write` still does not authorize a write. Writes go through
`validate_step_up_token` / `r6.access.require_grant`, and clinical writes
additionally require the human confirmation in the action rail. Nothing in
this design touches that chain, which is why `scopes_supported` is read-only:
advertising a write scope would imply an authority the scope does not carry.

---

## 4. The four architecture-review questions

### 4.1 Does this serve the vision, or is it adjacent work that feels productive?

It serves it, and it serves a smaller part of it than the framing suggests.

The thesis is an enforcement layer that lets an agent be useful on real health
records without being trusted. An enforcement layer no client can reach
enforces nothing, so distribution is not adjacent to the thesis — it is a
precondition for it. Feature set 6 exists for that reason.

The honest qualifier: what this design delivers, complete and deployed, is a
standards-conformant authenticated path to the **synthetic demo tenant**. It
does not reach real records (§6.1), and the reason is a consent surface that
does not exist. So it serves the distribution half of the thesis and not the
real-records half.

That is still worth building, for three reasons that are checkable rather than
rhetorical: it removes a refusal that actively misleads a partner; it exercises
the entire OAuth chain end to end so that the consent screen, when it is
built, drops into a pipeline that is known to work; and it is the difference
between a documented feature that cannot work and one that works within a
stated boundary. #290 makes the first point in the partner's own words.

### 4.2 What is the honest failure mode, and who notices it first?

There are two, and they have opposite visibility.

**The loud one: metadata that is confidently wrong.** A PRM whose `resource`
does not match the connect URL, or an `authorization_servers` value whose
metadata does not resolve at a location the client tries, produces a client
error that reads to the partner as our failure. It is strictly worse than
today's 404, because today's dead end at least looks like an absent feature
rather than a broken one. **Noticed first by: a partner, in a connector error
dialogue** — the same way #290 was reported ("It keeps saying Couldn't
register with HealthClaw's sign-in service"). §3.3 exists because today's
authorization server already has three separate instances of this defect.

**The quiet one, and the worse one: audience validation that is implemented
and never exercised.** A SMART token minted for the FHIR resource, replayed at
the MCP endpoint, is accepted. Nothing breaks. Nothing alerts. The lock has a
second key that nobody knows about, and the code review passes because the
audience check is right there in the diff. **Noticed first by: nobody.**

This repository's own history is the argument. `docs/2026-08-16-hard-truths.md`
§4 and §5 record five polished artifacts making false claims and three green
guards written narrower than the property they were named after. The shape is
always the same: the check exists, the check was never run against the case it
was written for, and the green tick then certifies the gap. That is why the
cross-audience replay in §8 is a **required** assertion and not a nice-to-have,
and why it is written from the property ("a token for another audience is
refused") rather than from the fix.

### 4.3 What does it make harder later?

Four things, stated so they are not surprises.

1. **The canonical resource URI becomes load-bearing.** Once tokens carry it
   as `aud` and partners have it saved, changing it invalidates both. This is
   why §3.1 is an owner decision made before phase 1 rather than a default
   picked by whoever writes the route.
2. **Flask joins the hot path of every OAuth-credentialed MCP call.** A Flask
   outage becomes an MCP outage for those clients. Two services now have to be
   up for one surface to work, and #155 says MCP deploy drift is already
   unobservable.
3. **Two credential types double the auth matrix.** Every future change to
   the MCP server has to be tested against the static-token path *and* the
   OAuth path. Hard-truths §5 is a list of guards that covered one path and
   certified the other. This is that risk, deliberately taken on.
4. **It creates an expectation it does not satisfy.** "You can connect from
   claude.ai" will be heard as "you can reach your records from claude.ai."
   Unless this document, `docs/quickstarts/claude.md`, and the #290 closing
   comment all state the demo-tenant boundary explicitly, we reproduce
   hard-truths §4 with a new artifact.

### 4.4 How will we prove it works, with what data, run by whom?

§8. The gate blocks on that section, not this one.

---

## 5. What this design assumes, and where the assumption is verified

| Assumption | Verified how |
|---|---|
| Production Flask requires tenant-bound credentials on protected reads | `r6/runtime_config.py:105` raises `READ_AUTH_ENABLED must be true in production` at boot. Read, not run — the production env is not readable from here. |
| The authorization server cannot mint a token for a protected tenant | `r6/oauth.py:286` — `authorize` returns 403 `access_denied` when `read_auth_enabled()` and the tenant is not public. Combined with the row above, this is why §6.1 is the scope boundary. |
| The demo tenant is reachable without a credential | `authorize_tenant_read` returns public tenants unconditionally (`r6/read_auth.py:63`). |
| The hosted tool catalogue is 27 tools | `adapters/tools.manifest.json` advertises **29**; `PRIVILEGED_TOOL_NAMES` withholds 2 (`fhir_get_token`, `fhir_seed`) from hosted transports, leaving **27**. Both numbers are named in §8 because the count is evidence. |

---

## 6. What this does NOT do — the scope boundary

Stated as a list because a boundary that is only implied is not a boundary.

### 6.1 It does not give a hosted connector access to a real tenant

This is the headline exclusion. Production Flask enforces
`READ_AUTH_ENABLED=true` at boot, and `/r6/fhir/oauth/authorize` refuses to
auto-approve any tenant that is not in `PUBLIC_TENANTS`. So the strongest
token this design can produce, through a fully conformant OAuth flow, is one
bound to a synthetic demo tenant.

The blocker is not OAuth. It is that `authorize` **has no consent screen** —
it auto-approves and takes the tenant from a request header. Pointing a
standards-conformant discovery chain at an auto-approving authorization server
that could bind any tenant would convert the lock into decoration. The 403 at
`r6/oauth.py:286` is the only thing preventing that today, and this design
depends on it rather than removing it.

**A consent surface for protected tenants is the follow-on specification**
(#568, "MCP authorization phases 2 and 3: a hosted connector still cannot
reach a tenant") and the thing that actually closes the distance to real
records. It is out of scope here, deliberately, because it is a product and
UX decision with a PHI boundary attached, and it does not fit in the same PR
as a metadata document.

### 6.2 It does not weaken or replace `MCP_AUTH_TOKEN`

The static credential stays required at boot and stays accepted. This design
is strictly additive.

### 6.3 It does not implement Client ID Metadata Documents

CIMD is the 2026-07-28 preference. DCR is deprecated but spec-legal and
retained precisely for authorization servers like ours, and clients fall back
to it when `client_id_metadata_document_supported` is absent. CIMD also
requires the authorization server to fetch attacker-supplied URLs, which is an
SSRF surface we would have to design against. Not in this scope.

### 6.4 It does not change the demo endpoint

`mcp-demo-production-ee2c` keeps running unauthenticated and tenant-pinned.
Measured working: unauthenticated `initialize` returns `200` with a session id.

### 6.5 It does not add a write path

No new write authority, no new step-up mechanism, no change to human
confirmation. §3.6.

### 6.6 It does not fix #155, #289, #427, or #57

Deploy drift, prod-watch origin coverage, the stale build, and the 1.8k-line
`tools.ts` are all real and all outside this diff. #289 becomes *more*
relevant if §3.1 selects a custom origin, and that is noted in §9.

### 6.7 It does not make the MCP server a token issuer

The MCP server validates tokens. It never mints them. There is exactly one
issuer.

### 6.8 It does not issue refresh tokens (amendment P2-f)

State it rather than leave it to be discovered. There are no refresh tokens:
`token` returns `access_token`, `token_type`, `expires_in` and `scope`, and
`refresh_token` appears nowhere in `r6/oauth.py`. `OAUTH_TOKEN_TTL` defaults to
3600 (`r6/oauth.py:33`), so an access token lives an hour, and a hosted
connector re-consents when it expires. That is spec-legal under MCP 2026-07-28
and it is a real cost to the person using the connector.

The consequence is worth naming because it lands on a patient, not on us: a
connector that worked an hour ago asks for consent again, with no explanation
that anything expired. Hourly re-consent is acceptable for a synthetic demo
tenant and is not a shape to carry into real records.

If refresh tokens are added later for public clients, rotation is a **MUST**
per OAuth 2.1 §4.3.1: each refresh returns a new refresh token and invalidates
the old one, and reuse of a rotated token revokes the chain. A non-rotating
refresh token issued to a public client is a long-lived bearer credential
stored on someone else's machine.

---

## 7. Migration path — production is never unlocked, not even briefly

Four phases. The invariant below is testable at every commit and every config
state in every one of them.

> **Invariant.** At no commit, and in no configuration state, does an
> unauthenticated `initialize` or `tools/call` against the production origin
> return anything other than `401`.

**Phase 0 — decide the canonical resource URI (§3.1).** No code. Ruled by D4:
`https://mcp.healthclaw.io/mcp`. Repoint DNS to the Railway service and verify
over DoH before phase 1 deploys; the record currently answers from Vercel with
`DEPLOYMENT_NOT_FOUND` (§9.6, #522).

**Phase 1 — serve the PRM and enrich the 401.** MCP server only. Two new
unauthenticated routes and two extra `WWW-Authenticate` parameters. **No
change to who is admitted**: unauthenticated calls still 401, non-matching
bearers still 401, `MCP_AUTH_TOKEN` still required at boot. This phase alone
closes the "no way forward" half of #290 — a client stops asking for a Client
ID that does not exist and instead reports honestly that it cannot obtain a
token. Independently shippable and independently valuable.

Phase 1 merges behind `MCP_CANONICAL_RESOURCE` and ships inert: unset, the
routes 404 and the challenge stays bare (§3.1, amendment P1-b). Merging is
therefore not deploying, and the constant is set only after DNS answers from
Railway. Phase 1 closes #523. #290 stays open until the §8.4 end-user run.

**Phase 2 — authorization server work.** Flask only. Issuer scheme fix, root
discovery route, `resource` recorded as `aud`, `invalid_target` rejection,
introspection endpoint, RFC 9207 `iss`. **The MCP server's behaviour does not
change at all in this phase.** Tokens gain an audience that nothing yet checks;
no new caller is admitted anywhere.

**Phase 3 — the MCP server accepts authorization-server tokens.** Behind
`MCP_OAUTH_ENABLED`, default `false`. Enabled in staging first, where the §8
walkthrough runs in full including the refusal assertions. Production
enablement requires explicit founder authorization, as every MCP server deploy
already does.

**Rollback is `MCP_OAUTH_ENABLED=false`.** The static-token path is untouched
in every phase, so rollback never involves removing a lock or restoring one.
There is no state in which the endpoint is open while a fix is prepared.

Phases 1 and 2 are commutative and can ship in either order. Phase 3 requires
both.

---

## 8. How we prove it works — the artifact, the data, and the runner

The architecture review blocks on this section. If it has no answer, the
design is not finished.

**Runner:** owner-surfaces writes and runs it. QA re-runs it adversarially and
owns §8.3, whose job is to find a fourth way in that the author did not think
of. Neither sign-off is the author's.

**Data:** the synthetic public demo tenant, only. This is not a precaution, it
is a property of §6.1 — no PHI is reachable through this design at all, by
construction. The recording therefore needs no redaction and there is no
"could not be recorded PHI-free" caveat to write.

**Artifact:** `services/agent-orchestrator/qa/oauth-walkthrough.sh`, in the
pattern of `examples/aidbox-healthclaw-guardrails/scripts/walkthrough.sh` — it
asserts, it fails loudly, and each failure names the guarantee that broke.
Plus a Playwright test in the pattern of
`examples/aidbox-healthclaw-guardrails/qa/` that makes the same real calls and
renders each result as it lands, so a video showing a pass cannot exist unless
the pass happened.

### 8.1 The positive chain — assert we received the thing, then assert its contents

Ordered. Each step asserts receipt before it asserts anything about content,
because "no PHI in the response" and "the audience is wrong" are both true of
an error.

| # | Assertion | Names the guarantee |
|---|---|---|
| A1 | Unauthenticated `POST /mcp` returns **401**, and `WWW-Authenticate` contains `resource_metadata=` **and** `scope=`, and contains **no** `error=` | the refusal offers a way forward |
| A2 | `GET` the `resource_metadata` URL **from the A1 header** — the sub-path form, never the root form — returns **200**, `content-type: application/json`, parseable, with `resource`, `authorization_servers`, `scopes_supported` present | the way forward exists |
| A3 | PRM `resource` at that same sub-path URL **string-equals** the URL used in A1 | the audience the client will request is the audience we will check |
| A4 | AS metadata resolves at one of the locations the spec requires clients to try, derived from `authorization_servers[0]`; its `issuer` string-equals `authorization_servers[0]`; every endpoint URL begins `https://`; `code_challenge_methods_supported` contains `S256` | the authorization server is discoverable and usable |
| A5 | DCR at `registration_endpoint` returns **201** with a `client_id` | a client with no prior relationship can register |
| A6 | authorize + token, PKCE `S256`, `resource=<RESOURCE>` — **follow the `302` `Location` header** from `/oauth/authorize` and read `code`, `state` and `iss` from its query, never from a JSON body; then assert an access token was **received**, then assert `token_type` is `Bearer` | a token can be obtained the way a browser would obtain it |
| A7 | `initialize` with that token returns **200** with an `Mcp-Session-Id`; `tools/list` returns **200** with **27** tools — and the log prints both numbers, 29 advertised in the manifest and 27 exercised on this transport, with the 2 privileged names | every advertised tool is served, and the count is stated rather than implied |
| A8 | every one of the 27 is called with valid arguments and returns a result | *answers* — the second half of the PRD's definition |

A4 is the assertion most likely to fail today. It is the gate: if A4 fails,
nothing deploys.

A8 is where the run cost lives, and it is the one the PRD explicitly demands —
"a tool listed and never called is the green check whose subject never ran,
with a menu."

### 8.2 The refusal chain — the half that goes unchecked

| # | Assertion | Names the guarantee |
|---|---|---|
| R1 | No credential → **401** | the lock holds |
| R2 | A random string as bearer → **401** with `error="invalid_token"` | garbage is not a credential |
| R3 | **A token minted at the same issuer with `resource=https://app.healthclaw.io/r6/fhir`, replayed at the MCP endpoint → 401 `invalid_token`** | audience validation is real |
| R4 | An expired token → **401** `invalid_token` | expiry is enforced at the resource, not only at the issuer |
| R5 | `MCP_AUTH_TOKEN` still returns the full tool list | the lock was added to, not replaced |
| R6 | Boot with neither `MCP_AUTH_TOKEN` nor the demo flag under `NODE_ENV=production` → **refuses to start** | fail-closed survived the change |
| R7 | `MCP_OAUTH_ENABLED=false` → an otherwise valid OAuth token gets **401** | the rollback switch actually rolls back |
| R8 | On the OAuth path, `_tenantId`, `_stepUpToken` and `_authorization` passed as **tool arguments** reach Flask in no form: no credential carried in a tool argument reaches Flask on the OAuth path | the header rule is not the only door (§3.6, amendment P3-a) |

**R3 is the assertion this whole design lives or dies by.** It is the only one
that distinguishes "we wrote an audience check" from "the audience check
works", and per §4.2 it is the failure nobody would otherwise notice. It is
written from the property, not from the fix: any token whose audience is not
`<RESOURCE>` is refused, regardless of how it was obtained.

### 8.3 The adversarial pass — QA's, not the author's

Not scripted in advance, by design. At minimum: audience string near-misses
(trailing slash, case, `http` vs `https`, a percent-encoded variant), a token
replayed after revocation, an introspection response that says `active: true`
with no `aud` field at all, a `resource` parameter naming an unregistered
target at `/oauth/token`, and a PRM fetched with a spoofed `Host` header to
confirm `resource` does not follow it.

### 8.4 Sign-offs

- **QA (adversarial):** ran §8.2 and §8.3, tried to make it lie, found nothing
  that admits an unauthenticated or wrong-audience caller.
- **End-user (not us):** a partner adds the connector from claude.ai's own UI,
  unaided, and reaches the demo tenant.

The end-user run is **the only place any client's actual behaviour is
measured.** This document asserts only what the specification requires of
clients. It makes no claim about what claude.ai, ChatGPT, or Perplexity
actually do, because nobody here has run them against this design — it does
not exist yet. Writing "claude.ai will now connect" before that run is exactly
the hard-truths §4 move, and it is not made here.

---

## 9. Failure modes and risks

The first three are the ones the review asked for. The rest were measured
while writing this and are recorded here rather than fixed, per the scope of
this branch. Each is a candidate row for feature set 6's edge-case register.

### 9.1 A client that ignores the metadata

Behaviour is unchanged from today: it presents a static header and succeeds,
or presents nothing and gets 401. No regression is possible, because nothing
was removed. Machine-to-machine integrations using `MCP_AUTH_TOKEN` are in
this category and are unaffected. Noticed first by: nobody, correctly.

### 9.2 A token for another audience

Refused with 401 `invalid_token` (§3.5), asserted by R3. The dangerous variant
is not rejection but *silent acceptance*, which is what happens today at the
Flask surface: `_oauth_authorizes` checks tenant and scope, not audience. That
is not a defect today, because no MCP-audience token exists. It becomes one
the moment phase 2 ships, which is why §3.6 forbids forwarding the header.

### 9.3 The metadata endpoint itself is wrong

The highest-severity mode, because it is confidently wrong and the client's
error reads as ours. Four sub-modes, three of which are **already present** in
the authorization server today and measured in §3.3:

| Sub-mode | Status today | Detected by |
|---|---|---|
| `resource` ≠ the URL the client connected to | would be new | A3 |
| AS metadata does not resolve at a location clients try | **present** (three 404/400s measured) | A4 |
| `issuer` inside AS metadata ≠ the `authorization_servers` value | **present** (`http://app.healthclaw.io`) | A4 |
| `http://` scheme on AS endpoints | **present** (every URL) | A4 |
| `resource_documentation` points at a URL that errors | **present** | not detected — A2 stops at the PRM |

Mitigation beyond A3/A4: both documents are pinned as fixtures in a test that
fetches the **live** documents and diffs them, so drift between the deployed
document and the design fails CI rather than a partner's connector.

**The fifth row is ours, and it is measured.** §3.2's document sends a partner
to `https://app.healthclaw.io/r6/fhir/docs/privacy-policy`. Fetched
unauthenticated on 2026-09-03, that returns `400` with
`{"issue":[{"code":"security","diagnostics":"X-Tenant-Id header is required"…`
— the tenant gate stands in front of the page, and the caller reading it has no
tenant yet by construction. So the one human-readable pointer in an otherwise
machine-facing document is the §9.3 mode in miniature: a URL we publish, that
errors, in the document that exists to stop a partner hitting a dead end.
`resource_documentation` is OPTIONAL in RFC 9728 §2, so the choices are to
exempt that route from the tenant requirement, point the field at a page that
answers unauthenticated, or drop the field. Not decided here, and not phase
1's to decide — phase 1 serves the value this section specifies. Whichever is
chosen belongs in the pre-deploy checks alongside the four curls, because the
document is inert until `MCP_CANONICAL_RESOURCE` is set.

### 9.4 CORS — measured, and it can make the whole design invisible

In `services/agent-orchestrator/src/index.ts` the auth middleware is installed
before the CORS middleware — named by position rather than by line, because
phase 1 inserts code above both and a line number here would go stale the way
P3-a's did. The 401 is therefore returned **before** any CORS header is set.
Measured, with `Origin: https://claude.ai`:

```
POST /mcp     -> 401, and NO access-control-allow-origin header
OPTIONS /mcp  -> 204, and NO access-control-allow-origin header
```

`ALLOWED_ORIGINS` holds 2 entries (per live `/health`) and `claude.ai` is not
among them. If any client fetches from a browser context, it cannot read the
401 *or* the `WWW-Authenticate` header, and this entire design is invisible to
it.

**Required:** the two PRM paths and the 401 response must carry
`Access-Control-Allow-Origin: *` and `Access-Control-Expose-Headers: WWW-Authenticate`.
This is safe and is not a widening of the lock: these responses contain no
secret, identify no tenant, and grant nothing — a `401` readable by a browser
is still a `401`. The transport endpoint's origin allowlist is untouched.

**Half of that shipped in phase 1, and the half that did not needs more than
one header.** The two PRM paths carry `Access-Control-Allow-Origin: *`, so the
metadata document itself is readable from a browser context. The 401 does not,
so the challenge that points at it is not. Adding the header to the 401 alone
would not fix that: a browser's `POST /mcp` with `content-type: application/json`
is preflighted, and `OPTIONS /mcp` also answers without
`Access-Control-Allow-Origin` — measured on the phase 1 build, 2026-09-03 — so
the browser never reaches the 401 to read a header on it. The refusal and its
preflight have to be fixed together, which is why phase 1 did not do either.
Tracked as part 2 of #523. A control that carried the header without the
preflight would look like the fix and not be one.

Whether any given hosted connector fetches server-side (where CORS does not
apply) is **not asserted here**. It is measured in the §8.4 end-user run. Phase
1's reachability claim therefore covers a server-side fetcher, not a
browser-context one.

### 9.5 Protocol version

Live `/health` reports `supportedProtocolVersions: ["2024-11-05"]`, and the
demo endpoint negotiated `2024-11-05` when probed with `2025-06-18`.
Authorization discovery is HTTP-level and version-independent, so this does
not block the design. It is a risk note: a client that requires ≥2025-06-18
before attempting authorization may never reach the 401 handling at all, and
2026-07-28 replaces the initialize handshake with `server/discover` and an
`MCP-Protocol-Version` header. Protocol-version currency belongs to feature
set 6 but not to this specification. **Candidate issue, not filed in this
branch.**

### 9.6 `mcp.healthclaw.io` is a dangling DNS record

Measured. It resolves (DoH: `216.150.16.129`, `216.150.1.193`), the response
is served by **Vercel**, and it returns `x-vercel-error: DEPLOYMENT_NOT_FOUND`.

Two consequences. It is the natural canonical resource URI (§3.1) and must be
repointed before phase 1 if chosen. Independently, a hostname on our domain
that resolves to a shared platform with no deployment attached is a
subdomain-takeover surface, on a domain that appears in health-product
documentation. **This is a real defect found while writing this document. It
is recorded here with its evidence and is not fixed in this branch, per the
scope. It should be filed and it is not this design's to close.**

### 9.7 Introspection availability

Every OAuth-credentialed call now depends on Flask. Flask down → those calls
401 (fail-closed, correct). Mitigation is caching under the token's SHA-256
with a TTL shorter than the token's own, which bounds the blast radius without
extending any token's life. See also §4.3 item 2.

### 9.8 The authorization server's auto-approve

`authorize` auto-approves with no consent screen and takes the tenant from
`X-Tenant-Id`. The **only** thing preventing a caller from minting a token for
an arbitrary tenant is the 403 at `r6/oauth.py:286`, which is conditional on
`read_auth_enabled()`. Production sets it at boot
(`r6/runtime_config.py:105`), so the guard holds there.

Two things follow, and both are stated rather than assumed. First, **a
non-production deployment of the Flask app with `READ_AUTH_ENABLED` unset and
this design's discovery chain in place would allow any caller to obtain a
token for any tenant.** Any such environment must not serve the PRM. Second,
this is a single conditional standing between a public discovery chain and
arbitrary tenant binding — thin enough that the consent surface (§6.1) should
not be deferred indefinitely on the grounds that the guard holds.

I have **not** verified `READ_AUTH_ENABLED`'s value in the running production
environment. The boot-time assertion is read from source, not observed. That
verification belongs in phase 2's pre-deploy checklist and is listed in §10.

---

## 10. Open questions for the review

1. ~~**§3.1 — which canonical resource URI?**~~ **Ruled** by D4:
   `https://mcp.healthclaw.io/mcp`, issuer `https://app.healthclaw.io`.
2. **Confirm `READ_AUTH_ENABLED=true` in the running production Flask**, by
   observation and not by reading `runtime_config.py`. §9.8 explains why this
   matters more than it looks. Pre-deploy checklist item for phase 2, now
   alongside `APP_ENV=production` and the demo tenant's membership of
   `PUBLIC_TENANTS` (§3.5.1).
3. **Which resource identifiers go in the `invalid_target` allowlist?** At
   minimum `<RESOURCE>` and the FHIR resource. Anything else is a decision.
   Amendment P2-b makes each entry carry a tenant policy, so adding one is a
   decision about which tenant it binds, not only about which audience is
   known.
4. ~~**Does closing #290 require phase 3?**~~ **Ruled** by D4: phase 1 closes
   #523. #290 closes on the §8.4 end-user run, and #568 tracks the consent
   work that reaches a real tenant.

---

## 11. Related

- `docs/prd/06-surfaces.md` — feature set 6, §6 of which is the SOW item this
  document answers
- `docs/2026-08-16-delivery-process.md` — step 3, the gate this document is
  submitted to
- `docs/2026-08-16-hard-truths.md` §4, §5 — the failure patterns §4.2 and §8
  are written against
- #523 (what phase 1 closes), #290 (this design), #522 (the dangling record
  phase 1 waits on), #164 (distribution epic), #155, #289, #243, #427
- `docs/2026-09-02-council-ruling.md` §D4 — the ruling these amendments carry
- #567 — URLs built from `request.host_url` lose the scheme behind the proxy
- #568 — the successor to #290: a hosted connector reaching a tenant, which
  is phase 3 plus the consent surface of §6.1
- MCP specification 2026-07-28 (Current), `basic/authorization`
- RFC 9728, RFC 8707, RFC 8414, RFC 7591, RFC 7662, RFC 9207, RFC 6750

---

## 12. Amendments 2026-09-02

Adopted by the council on 2026-09-02 (Interop seat, ruling D4). Each is edited
into the section it belongs to; this list is the index, not the content. Where
an amendment corrects the document rather than the design, it says so.

Every claim the P2 amendments make about what `r6/oauth.py` and
`r6/read_auth.py` do today was checked against the source on 2026-09-03, and
each now carries a `file:line`. **Read, not run** — this is source reading, not
a measurement against a deployment, and it is dated because line numbers move.
All seven held: the JSON authorize response, the tenant taken from
`X-Tenant-Id`, the absent client authentication, the absent `resource`
handling, the absent introspection endpoint, the 3600-second token with no
refresh, and the missing `aud` check in `_oauth_authorizes`.

**Phase 1 — merged behind `MCP_CANONICAL_RESOURCE`.**

- **P1-a — §3.2, §3.4.** The `resource_metadata` URL in the 401 is built from
  `MCP_CANONICAL_RESOURCE`, never from `req.hostname`. The document already
  said this for the PRM `resource`, and the header needs the same rule. The
  reason is RFC 9728 §3.3, not a proxy rewriting Host — Railway preserves
  Host, measured 2026-09-03. The scheme is what the proxy loses (#567).
- **P1-b — §3.1, §7.** The fail-closed rule as written could not work for
  phase 1: it keys on OAuth acceptance, which phase 1 predates. Replaced by
  the flag rule — unset means 404 and a bare challenge, set means both ship,
  malformed refuses to boot, and either is served only on the canonical Host.
  The reason for the Host gate is RFC 9728 §3.3.
- **P1-c — §3.4.** Citation fix. RFC 6750 §3.1 says a server SHOULD NOT send
  `error` on a credential-less 401. The document said "does not permit". The
  behaviour is unchanged.
- **P1-d — §3.2, §8.1.** "Byte-identically at both paths" is not conformant
  for the root form: root-form insertion implies the resource identifier
  `https://mcp.healthclaw.io`, and the document says `…/mcp`. MCP 2026-07-28
  tolerates it as the listed fallback, so both keep being served, and A2/A3
  now assert against the header-pointed sub-path URL.

**Phase 2 — authorization server, not yet built.**

- **P2-a — §3.5, §8.1.** `/oauth/authorize` MUST answer `302` with
  `Location: <redirect_uri>?code=…&state=…&iss=…` (OAuth 2.1 §4.1.2, RFC 9207
  §2). Today it returns JSON, which no browser flow can complete. A6 follows
  the `Location` header.
- **P2-b — §3.5.1.** Tenant binding for a browser-initiated authorize is
  config: when `resource` string-equals `MCP_CANONICAL_RESOURCE`, the tenant
  is `MCP_OAUTH_DEMO_TENANT` and `X-Tenant-Id` is ignored. The demo tenant
  must be in `PUBLIC_TENANTS`, and the `invalid_target` allowlist becomes a
  map from resource to tenant policy rather than a set.
- **P2-c — §3.5.** RFC 8707: the `resource` at `/oauth/token` MUST equal the
  one recorded on the code at `/oauth/authorize`, or be absent and inherit it.
  `invalid_target` applies at both endpoints.
- **P2-d — §3.5.** RFC 7591 at `/oauth/register`: add
  `client_secret_expires_at`, honour `token_endpoint_auth_method: none`,
  reject a `redirect_uri` that is neither `localhost` nor `https://`, and
  require `client_id` at `/oauth/token` for public clients (RFC 6749 §4.1.3).
- **P2-e — §3.5.** RFC 7662 §2.1: introspection must be protected. The
  credential is named now — `MCP_INTROSPECTION_CLIENT_ID` and
  `MCP_INTROSPECTION_CLIENT_SECRET`, a pre-registered confidential client,
  compared with `hmac.compare_digest`.
- **P2-f — §6.8.** State the token lifetime: no refresh tokens,
  `OAUTH_TOKEN_TTL=3600`, and a hosted connector re-consents hourly. If
  refresh is added later for public clients, rotation is a MUST (OAuth 2.1
  §4.3.1).

**Phase 3 — MCP server accepts those tokens, not yet built.**

- **P3-a — §3.6, §8.2.** §3.6 stopped header passthrough in `extractHeaders`
  and left the JSON-RPC tool-argument overrides open (the
  `CallToolRequestSchema` handler in
  `services/agent-orchestrator/src/index.ts`). On the
  OAuth path `_tenantId`, `_stepUpToken` and `_authorization` are discarded,
  exactly as `isPublicDemo()` pins them. Asserted as R8.

---

## 13. Amendment 2026-09-06: the consent surface, and a hosted connector reaching a real tenant (#568)

**Status: proposed by the founder on 2026-09-06; design only in this section.**
The founder asked for the locked server to work as an ordinary claude.ai
connector on real records, with CareAgents as the account and consent surface.
That request overrides the "phase 2 waits" clause of ruling D4. Everything in
§1 to §12 stands; this section fills the one hole §6.1 and #568 left open.

### 13.1 What claude.ai actually does, from Anthropic's own connector notes

Read, not run, on 2026-09-06. Measured against our deployment only in the §8.4
end-user run.

- Claude registers itself with Dynamic Client Registration. The client name is
  `Claude`. The callback is `https://claude.ai/api/mcp/auth_callback`, and
  Anthropic says it may become `https://claude.com/api/mcp/auth_callback`, so
  both are accepted as registered redirect URIs.
- The authorize request carries `resource=` (the PRM `resource`, RFC 8707),
  `scope=` (from the PRM), `code_challenge` with `S256`, and `state`. Claude
  supports the 2025-03-26 and 2025-06-18 authorization specs, which is the
  discovery chain §3 designs to.
- Claude Code, as a native client, registers loopback redirect URIs
  (`http://localhost/callback`, `http://127.0.0.1/callback`) on ephemeral
  ports; redirect-URI matching for `localhost` and `127.0.0.1` must ignore the
  port.
- A manual client id and secret can be pasted under "Advanced settings"; that
  path is not designed for and not needed.
- Whether Claude uses the refresh grant is not stated. The server offers it
  (§13.5); the end-user run reports whether it was taken.

### 13.2 The shape: Flask stays the issuer, CareAgents is the front door

The issuer is unchanged (`https://app.healthclaw.io`, D4). The MCP server
still introspects at Flask (§3.5). What changes is what happens between a
browser arriving at `/oauth/authorize` and a code being issued:

```
claude.ai ── GET /r6/fhir/oauth/authorize?...&resource=<RESOURCE> ──▶ Flask
                Flask validates client, PKCE, resource; parks the request
                302 ─▶ https://careagents.cloud/authorize?req=<signed handle>
                                                                     │
      CareAgents: passkey sign-in; consent page lists the account's   │
      connections (each one a tenant) plus "the demo records";        │
      approval requires a fresh passkey assertion; Grant row written  │
                302 ◀─ /r6/fhir/oauth/consent/return?grant=<signed>  ─┘
                Flask verifies the grant, binds the code to its tenant,
                audits the consent under that tenant
                302 ─▶ https://claude.ai/api/mcp/auth_callback?code&state&iss
```

Two extra top-level redirects. From the client's side it is ordinary OAuth
2.1 with PKCE. CareAgents runs no authorization server: no registration, no
token endpoint, no PKCE. It authenticates a person and records a decision.

**The alternative, stated so it can be chosen instead:** CareAgents as the
issuer. It would port registration, PKCE, the stores and revocation from
`r6/oauth.py`, re-rule D4, and move the PRM's `authorization_servers`. Nothing
the founder asked for needs that.

### 13.3 The handoff protocol

Both sides already share `INTERNAL_TOKEN_MINT_SECRET` (CareAgents mints
step-up tokens with it). The handoff key is derived from it with domain
separation, the way `payload_digest` derives from `STEP_UP_SECRET`:
`sha256(b'healthclaw-consent-handoff:' + INTERNAL_TOKEN_MINT_SECRET)`. One
fewer secret to provision, and a forged grant needs the mint secret, which
already grants everything.

**Outbound (Flask to CareAgents).** Flask stores the parked request under a
random `request_id` (10 minutes, single use) with `client_id`, `client_name`,
`redirect_uri`, `scopes`, `code_challenge`, `state`, `resource`. The URL
carries `req=<request_id>.<exp>.<tag>`, where `tag` is HMAC-SHA256 over
`request_id.exp` under the handoff key. The page shows the client name and
scopes from the parked request, fetched by CareAgents from Flask
(`GET /r6/fhir/oauth/consent/<request_id>`, service-authenticated with the
mint secret), so nothing about the client rides in the URL.

**Inbound (CareAgents to Flask).** `grant=<base64url(JSON)>.<tag>` where the
JSON is `{request_id, tenant_id, consent_id, nonce, exp, decision}` and
`decision` is `approved` or `denied`. Flask: `compare_digest` on the tag;
`exp` in the future; `nonce` unused (the nonce cache in `r6/stepup.py`);
the parked request popped exactly once; `tenant_id` matches
`^[a-zA-Z0-9_-]{1,64}$`. Then the code is issued bound to `tenant_id`, or
the client is sent `error=access_denied`.

**Trust, written down as an assumption.** Flask cannot verify that the
CareAgents account owns `tenant_id`; it trusts the signed assertion. That
trust exists today: CareAgents holds the mint secret and can act on any
tenant. This section adds no authority, it moves an existing one onto a
signed, single-use, expiring message.

**The tenant policy of the `invalid_target` map (§3.5.1) gains a third value.**
`demo` binds `MCP_OAUTH_DEMO_TENANT`; `careagents` sends the request through
§13.3 and binds whatever the grant names; the FHIR resource keeps binding the
header tenant behind the existing public-tenant guard. With
`CAREAGENTS_CONSENT_URL` unset, the MCP resource falls back to `demo`, which
is the §3.5.1 behaviour unchanged.

### 13.4 Consent is a tenant event, and it can be taken back

- Flask writes an AuditEvent under the tenant on approval: `event_type=create`,
  `resource_type=Consent`, `resource_id=<consent_id>`, detail
  `client_id=… scopes=… via=careagents`. No account id, no email, no label.
- Flask stores the consent (`consent_id` to `{tenant_id, client_id, scopes,
  granted_at, revoked_at}`) beside the OAuth stores. Every access token and
  refresh token carries its `consent_id`.
- `POST /r6/fhir/oauth/consent/<consent_id>/revoke`, service-authenticated
  with the mint secret, sets `revoked_at`. Introspection answers
  `active: false` for every token of a revoked consent, and the refresh chain
  dies with it.
- CareAgents keeps a `Grant` row per approval (account, connection, client
  name, scopes, granted and revoked times; no PHI) and shows it on the home
  page with "Revoke", which calls the endpoint above.

### 13.5 Refresh tokens, because hourly re-consent on real records is not acceptable (§6.8)

The token endpoint gains `grant_type=refresh_token` for the clients registered
here. Refresh tokens are opaque, live 30 days, carry `consent_id`, `aud`,
`tenant_id`, `scopes` and `client_id`, and rotate on every use: the response
carries a new refresh token and the old one is dead. Presenting a rotated
token again revokes the whole chain (OAuth 2.1 §4.3.1). Public clients must
send their `client_id`; a refresh token is bound to the client it was issued
to. Discovery advertises `refresh_token` in `grant_types_supported`.

### 13.6 The downstream credential on the OAuth path

§3.6 leaves the MCP server without a credential for a protected tenant. The
MCP server already holds `INTERNAL_TOKEN_MINT_SECRET` and already has
`ensureReadToken`, which mints a step-up token per tenant. The gap is that
`/internal/step-up-token` accepts no scope, so that mint is write-capable.
Change: the mint endpoint accepts an optional `scope`, only `"read"` or
absent, and the OAuth path always sends `"read"`. `generate_step_up_token`
already produces read-scoped tokens and `authorize_tenant_read` already
accepts them; a read-scoped token can never authorize a write (H4). The
consent scopes are `fhir.read` and `context.read`, both reads, so the
downstream authority equals the consented one. The minted token is cached
under the SHA-256 of the OAuth token, never per tenant, for at most 5 minutes
and never past the OAuth token's own expiry, so a revoked consent stops
being served within the cache window.

Token exchange (RFC 8693) was the alternative and is the more standard shape;
it was not chosen because it adds a grant type and a second audience to the
same outcome.

### 13.7 What the person sees, and what is true about their identity

The consent page says who is asking (`Claude`), what they get (read access,
redacted, audited; never writes, which still need the human gate), and to
which records. Approval requires a fresh passkey assertion with
`user_verification=required`, not a remembered session. That is what makes
"biometric" a true word here. The tenants offered are the account's
connections, each created through a provider-portal sign-in via Fasten or
through the person's own upload. No identity-assurance level is claimed
anywhere; the page says what happened, not what it proves.

Consent to expose a real tenant to a third-party agent is gated by
`CARE_REAL_RECORDS` exactly as a new real-record connection is: `off` offers
only the demo records, `allowlist` offers real tenants to listed accounts,
`on` offers them to everyone. The founder can widen it later.

### 13.8 Delivery, and the gates that are not ours

Seven pull requests, each independently green and each behind the existing
flags: Flask conformance (§3.5 P2-a to P2-d, root discovery, `aud` in
`_oauth_authorizes`); introspection plus the mint scope; the handoff, revoke
and consent audit; refresh; CareAgents consent page and Grant; MCP server
phase 3 (`MCP_OAUTH_ENABLED`, drop passthrough and the tool-argument
overrides, read-scoped mint, CORS on the 401 and its preflight); the
walkthrough and this document's test plan.

Nothing in the list deploys. The owner's checklist before the §8.4 run:
repoint `mcp.healthclaw.io` and verify over DoH (#522); check the Vercel
domain verification; set `MCP_CANONICAL_RESOURCE`, `OAUTH_ISSUER`,
`MCP_INTROSPECTION_CLIENT_ID` and `_SECRET`, `CAREAGENTS_CONSENT_URL`; confirm
`READ_AUTH_ENABLED=true` by observation (§10.2); deploy Flask, the MCP server
and CareAgents web and worker from the same stage; then `MCP_OAUTH_ENABLED=true`
in staging first.
