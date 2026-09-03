"""Connector registry — the pluggable menu behind CareAgents' connection step.

Each connector knows how to *start* its flow; every path lands in the same
guarded HealthClaw tenant (redaction / audit / step-up inherited). Adding a
source = one CATALOG entry + (if it needs a live flow) one branch in `start`.
No template changes — the hub renders the marketplace from `catalog()`.

Tiers:
  live   a working connect flow now (sample, verified provider, wearables*)
  import paste / upload a shared record (SMART Health Link, FHIR file)
  soon   honest placeholder; "notify me" records intent, never a dead end

*Wearables (incl. Apple Health via Open Wearables) is only "live" where the
 deployment has the Open Wearables sidecar wired (CARE_WEARABLES_ENABLED) —
 Open Wearables' OAuth authorize still needs developer-session auth upstream,
 so we don't advertise a flow that would dead-end.
"""

from __future__ import annotations

# Providers Open Wearables can broker. Apple Health / Health Connect ride the
# same sidecar — Open Wearables owns the phone bridge, so we add no native code.
WEARABLE_PROVIDERS = [
    {"id": "apple", "label": "Apple Health"},
    {"id": "oura", "label": "Oura"},
    {"id": "whoop", "label": "Whoop"},
    {"id": "garmin", "label": "Garmin"},
    {"id": "fitbit", "label": "Fitbit"},
    {"id": "strava", "label": "Strava"},
]

_CATALOG = [
    {"id": "sample", "tier": "live", "icon": "🧪",
     "label": "Try it with sample records",
     "blurb": "An instant synthetic record — explore safely, no signup."},
    {"id": "fasten", "tier": "live", "icon": "🏥",
     "label": "Your provider (verified)",
     "blurb": "Log in to your clinic or hospital portal. Verified; we never "
              "see your password."},
    {"id": "wearable", "tier": "live", "icon": "⌚️",
     "label": "Apple Health & wearables",
     "blurb": "Oura, Whoop, Garmin, Fitbit, Strava, and Apple Health — "
              "through Open Wearables.",
     "providers": WEARABLE_PROVIDERS},
    # `direct` is the zero-integration ingest path (#227): the signed-in
    # patient posts a FHIR Bundle they exported from another app or portal,
    # and the engine's `internal/ingest-bundle` endpoint applies the same
    # code path Fasten/SHC take. `shl` remains coming-soon until the
    # encrypted-manifest decoder ships (#225 follow-up) — the house rule is
    # ship the mechanism, then the copy.
    {"id": "shl", "tier": "soon", "icon": "🔗",
     "label": "SMART Health Link",
     "blurb": "Import a record shared with you as a SMART Health Link. "
              "Not open yet — we'll let you know."},
    {"id": "direct", "tier": "import", "icon": "📄",
     "label": "Upload records",
     "blurb": "Bring a FHIR bundle you exported from another app or your "
              "provider portal — we'll ingest it into a private tenant "
              "you control."},
    {"id": "healthex", "tier": "soon", "icon": "🧬",
     "label": "HealthEx",
     "blurb": "Connect your HealthEx account."},
    {"id": "hbo", "tier": "soon", "icon": "🏦",
     "label": "Health Bank One",
     "blurb": "Connect your Health Bank One vault."},
]

_BY_ID = {c["id"]: c for c in _CATALOG}
_WEARABLE_IDS = {p["id"] for p in WEARABLE_PROVIDERS}

# The sources that take a person's OWN records. Closed to new connections
# unless the deployment's CARE_REAL_RECORDS switch opens them for this
# account (council ruling 2026-09-02, D3) — the beta runs on synthetic data.
REAL_RECORD_SOURCES = ("fasten", "wearable", "direct")
_REAL_RECORDS_CLOSED = ("real-records connect isn't open on this beta "
                        "deployment yet — start with the sample records")


def catalog(cfg, real_records: bool = False) -> list[dict]:
    """The marketplace tiles with per-deployment availability resolved.

    `real_records` is whether the viewing account may START a real-record
    connection (`cfg.real_records_open_for(email)`); the app passes it. The
    default is closed so a caller that forgets fails safe.
    """
    out = []
    for c in _CATALOG:
        item = {k: c[k] for k in ("id", "label", "blurb", "icon", "tier")}
        if c["id"] in REAL_RECORD_SOURCES and not real_records:
            # Coming soon, honestly: no consent card (nothing to consent
            # to), and the tag says what a tester can do instead.
            item["tier"] = "soon"
            item["note"] = "coming soon"
            item["blurb"] = ("Not open in this beta — start with the "
                             "sample records.")
            if "providers" in c:
                item["providers"] = c["providers"]
            out.append(item)
            continue
        # Every live real-record source gets the consent card; sample
        # doesn't. `direct` (patient-provided FHIR bundle upload) is a
        # real-record source too — the file is the patient's own PHI, so
        # it rides the same consent gate as fasten/wearable.
        if c["id"] in REAL_RECORD_SOURCES:
            item["requires_consent"] = True
        if c["id"] == "fasten" and not getattr(cfg, "fasten_public_key", ""):
            item["tier"] = "soon"
            item["note"] = "not configured on this deployment"
        if c["id"] == "wearable":
            if getattr(cfg, "wearables_enabled", False):
                item["providers"] = c["providers"]
            else:
                item["tier"] = "soon"
                item["note"] = "Open Wearables sidecar not wired here yet"
                item["providers"] = c["providers"]
        elif "providers" in c:
            item["providers"] = c["providers"]
        out.append(item)
    return out


def get(connector_id: str) -> dict | None:
    return _BY_ID.get(connector_id)


def start(connector_id: str, provider: str | None, cfg, client,
          real_records: bool = False) -> dict:
    """Return a plan for the connection the app should persist, or a marker:

      {tenant, status, label, provider?, connect_url?}  — create this connection
      {"soon": True}                                    — record waitlist intent
      {"error": msg, "code": int}                       — refuse

    The app layer owns persistence (account scoping) and any seeding; `start`
    only decides the plan + builds provider URLs. `real_records` is as for
    `catalog()`: a closed real-record source is refused with 503 here, not
    waitlisted, so the hub never records intent for a tile the switch hid.
    """
    spec = _BY_ID.get(connector_id)
    if spec is None:
        return {"error": "unknown connector", "code": 404}

    if connector_id in REAL_RECORD_SOURCES and not real_records:
        return {"error": _REAL_RECORDS_CLOSED, "code": 503}

    if connector_id == "sample":
        # Synthetic data only — no personal data, so no consent gate. Keeping
        # the try-it path friction-free is deliberate (see beta-tester-guide).
        return {"tenant": client.new_tenant_id(), "status": "active",
                "label": "Sample records", "provider": "CareAgents sample",
                "seed": True}

    if connector_id == "fasten":
        if not getattr(cfg, "fasten_public_key", ""):
            return {"error": "real-records connect isn't configured on this "
                             "deployment yet", "code": 503}
        tenant = client.new_tenant_id()
        return {"tenant": tenant, "status": "pending",
                "label": "My health provider", "provider": "Connecting…",
                "requires_consent": True,
                "connect_url": client.fasten_connect_url(tenant)}

    if connector_id == "wearable":
        if not getattr(cfg, "wearables_enabled", False):
            return {"soon": True}
        prov = (provider or "").lower()
        if prov not in _WEARABLE_IDS:
            return {"error": "unknown wearable provider", "code": 400}
        label = next(p["label"] for p in WEARABLE_PROVIDERS if p["id"] == prov)
        tenant = client.new_tenant_id()
        return {"tenant": tenant, "status": "pending", "label": label,
                "provider": label, "requires_consent": True,
                "connect_url": client.wearables_connect_url(tenant, prov)}

    if connector_id == "direct":
        # Patient-provided real records: the tenant exists after connect but
        # holds nothing until the follow-up upload lands, so status starts as
        # `empty` (distinct from `pending` for OAuth flows, so the hub can
        # render "waiting for your file" rather than "waiting for the portal").
        tenant = client.new_tenant_id()
        return {"tenant": tenant, "status": "empty",
                "label": "Uploaded records", "provider": "Direct upload",
                "requires_consent": True}

    # remaining soon tiers: no live flow yet — record intent, never dead-end.
    return {"soon": True}


def refresh(connector_id: str, tenant: str, provider: str | None,
            cfg, client) -> dict:
    """Plan a re-pull of an EXISTING connection. Sibling of `start`.

    Returns one of:
      {"reauth_url": url, "requires_consent": bool}  — patient must re-authorize
      {"reingest": True}                             — server can re-pull alone
      {"unsupported": True, "reason": str}           — nothing to refresh
      {"error": msg, "code": int}                    — refuse

    Refreshing reuses the SAME tenant, which is what makes this safe to repeat:
    HealthClaw's ingest upserts on (tenant, resource_type, id), so a re-pull
    updates existing resources instead of duplicating them.

    We deliberately do NOT hold long-lived provider credentials to make this
    one-tap. Re-authorizing per refresh keeps PHI-capable refresh tokens out of
    this app entirely; the cost is one portal login, which the patient is
    already used to. Fasten is the exception — its connection is server-side
    and its webhook drives ingest, so it needs no patient round-trip.
    """
    spec = _BY_ID.get(connector_id)
    if spec is None:
        return {"error": "unknown connector", "code": 404}

    if connector_id == "sample":
        # Synthetic data is generated, not fetched — re-seeding would only
        # rewrite the same fixture. Say so rather than pretending to sync.
        return {"unsupported": True,
                "reason": "Sample records are synthetic — there's nothing new "
                          "to pull. Connect a real source to see updates."}

    if connector_id == "fasten":
        if not getattr(cfg, "fasten_public_key", ""):
            return {"error": "real-records connect isn't configured on this "
                             "deployment yet", "code": 503}
        # The Fasten connection lives server-side and its webhook ingests the
        # export, so the patient re-opens the same connect page and Fasten
        # re-runs against the connection it already holds.
        return {"reauth_url": client.fasten_connect_url(tenant),
                "requires_consent": False}

    if connector_id == "wearable":
        if not getattr(cfg, "wearables_enabled", False):
            return {"unsupported": True,
                    "reason": "Wearables aren't wired on this deployment yet."}
        prov = (provider or "").lower()
        if prov not in _WEARABLE_IDS:
            return {"error": "unknown wearable provider", "code": 400}
        return {"reauth_url": client.wearables_connect_url(tenant, prov),
                "requires_consent": False}

    return {"unsupported": True,
            "reason": "This source doesn't support refreshing yet."}
