"""SMBP reminder scheduler — Flask handler for GET /r6/smbp/reminders/due.

Read-shaped: tenant-read-authenticated + AuditEvent (PHI-free: due count only).
Registered onto smbp_blueprint via register_scheduler_routes, mirroring the
register_*_routes pattern in r6/quality and r6/sdc.

The response is a PHI-safe overview — patient_ref, language, and a `has_contact`
boolean, NEVER the phone number. The number lives only inside the reminder
payloads that the action layer (propose -> human-confirm -> commit) consumes,
which this endpoint does not emit.
"""

import logging
from datetime import datetime, timezone

from flask import request, jsonify

from r6.models import R6Resource
from r6.audit import record_audit_event
from r6.smbp.models import SMBPSession
from r6.smbp.scheduler import due_reminders, contact_phone

logger = logging.getLogger(__name__)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_effective(value):
    """Parse a FHIR effectiveDateTime to naive UTC, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def register_scheduler_routes(blueprint, deps):
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]

    def _tenant():
        return (request.headers.get("X-Tenant-Id") or "").strip() or None

    @blueprint.route("/reminders/due", methods=["GET"])
    def reminders_due():
        tenant_id = _tenant()
        if not tenant_id:
            return jsonify(operation_outcome(
                "error", "security", "X-Tenant-Id required")), 400
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]

        now = _utcnow()

        # Per-patient reading activity, computed from the tenant's Observations.
        # completed = reading count; last_reading_at = latest effectiveDateTime.
        # Live rows only: a deleted reading must not count toward the course
        # or pass for the latest one and silence a due reminder.
        counts = {}
        latest = {}
        for row in R6Resource.query.filter_by(
                resource_type="Observation", tenant_id=tenant_id,
                is_deleted=False).all():
            obs = row.to_fhir_json()
            ref = obs.get("subject", {}).get("reference")
            if not ref:
                continue
            counts[ref] = counts.get(ref, 0) + 1
            eff = _parse_effective(obs.get("effectiveDateTime"))
            if eff and (ref not in latest or eff > latest[ref]):
                latest[ref] = eff

        sessions = SMBPSession.query.filter_by(tenant_id=tenant_id).all()
        for s in sessions:
            # Transient attributes the pure engine reads via getattr.
            s.completed = counts.get(s.patient_ref, 0)
            s.last_reading_at = latest.get(s.patient_ref)

        reminders = due_reminders(sessions, now)

        # PHI-safe overview: label + language + a contact boolean, never a number.
        by_ref = {s.patient_ref: s for s in sessions}
        items = []
        for r in reminders:
            ref = r["patient_ref"]
            s = by_ref.get(ref)
            items.append({
                "patient_ref": ref,
                "kind": r["reminder_action_payload"]["kind"],
                "lang": getattr(s, "language", "en") if s else "en",
                "has_contact": bool(contact_phone(s)) if s else False,
            })

        record_audit_event("read", "SMBPSession", "reminders-due",
                           agent_id=request.headers.get("X-Agent-Id"),
                           tenant_id=tenant_id,
                           detail="smbp reminders due=%d" % len(items))

        return jsonify({"due": len(items), "reminders": items}), 200
