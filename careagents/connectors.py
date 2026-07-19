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
    {"id": "shl", "tier": "import", "icon": "🔗",
     "label": "SMART Health Link",
     "blurb": "Paste a SMART Health Link or scan its QR to import a shared "
              "record."},
    {"id": "direct", "tier": "import", "icon": "📄",
     "label": "Upload records",
     "blurb": "Drop in a FHIR bundle or a SMART Health Card file."},
    {"id": "healthex", "tier": "soon", "icon": "🧬",
     "label": "HealthEx",
     "blurb": "Connect your HealthEx account."},
    {"id": "hbo", "tier": "soon", "icon": "🏦",
     "label": "Health Bank One",
     "blurb": "Connect your Health Bank One vault."},
]

_BY_ID = {c["id"]: c for c in _CATALOG}
_WEARABLE_IDS = {p["id"] for p in WEARABLE_PROVIDERS}


def catalog(cfg) -> list[dict]:
    """The marketplace tiles with per-deployment availability resolved."""
    out = []
    for c in _CATALOG:
        item = {k: c[k] for k in ("id", "label", "blurb", "icon", "tier")}
        # Every live real-record source gets the consent card; sample doesn't.
        if c["id"] in ("fasten", "wearable"):
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


def start(connector_id: str, provider: str | None, cfg, client) -> dict:
    """Return a plan for the connection the app should persist, or a marker:

      {tenant, status, label, provider?, connect_url?}  — create this connection
      {"soon": True}                                    — record waitlist intent
      {"error": msg, "code": int}                       — refuse

    The app layer owns persistence (account scoping) and any seeding; `start`
    only decides the plan + builds provider URLs.
    """
    spec = _BY_ID.get(connector_id)
    if spec is None:
        return {"error": "unknown connector", "code": 404}

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

    # import + soon tiers: no live flow yet — record intent, never dead-end.
    return {"soon": True}
