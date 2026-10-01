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

import logging

logger = logging.getLogger(__name__)

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

# The menu's groups, in the order the hub shows them (calm hub spec
# section 4). The sample sits outside them: it is the closed state's one
# action and the open state's small link.
GROUPS = (("find", "Find my records"),
          ("services", "Record services"),
          ("file", "Bring a file"),
          ("devices", "Devices and apps"))

_CATALOG = [
    {"id": "sample", "tier": "live", "icon": "🧪", "group": "sample",
     "label": "Explore with made-up records",
     "blurb": "Made-up records to explore safely."},
    {"id": "fasten", "tier": "live", "icon": "🏥", "group": "find",
     "label": "Find my records at my doctor or hospital",
     "blurb": "Sign in to your patient portal. We never see your password."},
    {"id": "hbo", "tier": "soon", "icon": "🏦", "group": "services",
     "label": "Health Bank One",
     "blurb": "Connect your Health Bank One account."},
    {"id": "healthex", "tier": "soon", "icon": "🧬", "group": "services",
     "label": "HealthEx",
     "blurb": "Connect your HealthEx account."},
    # `direct` is the zero-integration ingest path (#227): the signed-in
    # patient posts a FHIR Bundle they exported from another app or portal.
    # `shl` stays coming soon until the encrypted-manifest decoder ships
    # (#225): ship the mechanism, then the copy.
    {"id": "direct", "tier": "import", "icon": "📄", "group": "file",
     "label": "Upload a file from your patient portal",
     "blurb": "A health record file you downloaded from your portal "
              "or another app."},
    {"id": "shl", "tier": "soon", "icon": "🔗", "group": "file",
     "label": "SMART Health Link",
     "blurb": "Import a record someone shared with you as a link."},
    {"id": "wearable", "tier": "live", "icon": "⌚️", "group": "devices",
     "label": "Apple Health and wearables",
     "blurb": "Oura, Whoop, Garmin, Fitbit, Strava and Apple Health.",
     "providers": WEARABLE_PROVIDERS},
]

_BY_ID = {c["id"]: c for c in _CATALOG}
_WEARABLE_IDS = {p["id"] for p in WEARABLE_PROVIDERS}

# The sources that take a person's OWN records. Closed to new connections
# unless the deployment's CARE_REAL_RECORDS switch opens them for this
# account (council ruling 2026-09-02, D3) — the beta runs on synthetic data.
REAL_RECORD_SOURCES = ("fasten", "wearable", "direct")
# The one sentence a refused tester reads, so it is a sentence: patient-facing
# copy here is plain, sentence-cased and em-dash-free. Parallel to the closed
# tile's own blurb ("Not open in this beta. Start with the sample records.").
_REAL_RECORDS_CLOSED = ("Connecting your own records isn't open in this beta "
                        "yet. Start with the sample records.")


def _closed_reason(connector_id: str, cfg) -> str:
    """Why an open real-record source still cannot start here. For the
    server log only: a person sees "Coming soon" (calm hub spec section 4)."""
    if connector_id == "fasten" and not getattr(cfg, "fasten_public_key", ""):
        return "FASTEN_PUBLIC_KEY is unset"
    if connector_id == "wearable" and not getattr(cfg, "wearables_enabled",
                                                  False):
        return "CARE_WEARABLES_ENABLED is off"
    return ""


def catalog(cfg, real_records: bool = False) -> list[dict]:
    """The menu with per-account availability resolved.

    `real_records` is whether the viewing account may START a real-record
    connection (`cfg.real_records_open_for(email, invited=...)`). The
    default is closed, so a caller that forgets fails safe. `chip` is the one status word each
    source shows; "Connected" is added by the hub, which knows the account.
    """
    out = []
    for c in _CATALOG:
        item = {k: c[k] for k in ("id", "label", "blurb", "icon", "tier",
                                  "group")}
        if "providers" in c:
            item["providers"] = c["providers"]
        if c["id"] in REAL_RECORD_SOURCES and not real_records:
            # No consent card (nothing to consent to), and the blurb says
            # what a tester can do instead.
            item["tier"] = "soon"
            item["note"] = "coming soon"
            item["blurb"] = ("Not open in this beta. Start with the sample "
                             "records.")
        else:
            # Every real-record source that can start gets the consent card.
            # `direct` is the patient's own PHI too.
            if c["id"] in REAL_RECORD_SOURCES:
                item["requires_consent"] = True
            reason = _closed_reason(c["id"], cfg)
            if reason:
                logger.info("connector %s shown as coming soon: %s",
                            c["id"], reason)
                item["tier"] = "soon"
                item["note"] = "coming soon"
        item["chip"] = "Coming soon" if item["tier"] == "soon" else "Available"
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
            logger.info("fasten start refused: FASTEN_PUBLIC_KEY is unset")
            return {"error": "Finding your records isn't available right "
                             "now.", "code": 503}
        tenant = client.new_tenant_id()
        return {"tenant": tenant, "status": "pending",
                "label": "Records from your doctor", "provider": "your doctor",
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
                "reason": "Sample records are made up, so there's nothing "
                          "new to pull. Connect a real source to see "
                          "updates."}

    if connector_id == "fasten":
        if not getattr(cfg, "fasten_public_key", ""):
            logger.info("fasten refresh refused: FASTEN_PUBLIC_KEY is unset")
            return {"error": "Finding your records isn't available right "
                             "now.", "code": 503}
        # The Fasten connection lives server-side and its webhook ingests the
        # export, so the patient re-opens the same connect page and Fasten
        # re-runs against the connection it already holds.
        return {"reauth_url": client.fasten_connect_url(tenant),
                "requires_consent": False}

    if connector_id == "wearable":
        if not getattr(cfg, "wearables_enabled", False):
            logger.info("wearable refresh refused: CARE_WEARABLES_ENABLED off")
            return {"unsupported": True,
                    "reason": "Refreshing this source isn't available yet."}
        prov = (provider or "").lower()
        if prov not in _WEARABLE_IDS:
            return {"error": "unknown wearable provider", "code": 400}
        return {"reauth_url": client.wearables_connect_url(tenant, prov),
                "requires_consent": False}

    return {"unsupported": True,
            "reason": "This source doesn't support refreshing yet."}
