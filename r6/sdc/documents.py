"""Persist a completed intake PDF as a FHIR DocumentReference.

persist_intake_document() embeds the actual PDF bytes (base64, per FHIR's
Attachment.data) in content[0].attachment.data — not just a byte count —
so the rendered PDF (r6/sdc/pdf.py::render_questionnaire_response_pdf) can
be retrieved later for delivery (Task 7) without re-rendering.

Mirrors r6/smbp/routes.py::_persist_document_reference's DocumentReference
shape, generalized to carry the bytes and to optionally link back to the
QuestionnaireResponse the PDF was rendered from.
"""

import base64
import json
from datetime import datetime, timezone

from models import db
from r6.models import R6Resource
from r6.audit import record_audit_event

# LOINC "Summarization of episode note" — a generic document-summary type
# code commonly used for rendered clinical/administrative documents (CCD-style
# summaries) when no more specific LOINC code applies. Mirrors the smbp
# report's use of a LOINC coding for its DocumentReference.type.
_INTAKE_DOCUMENT_TYPE = {
    "coding": [{"system": "http://loinc.org", "code": "34133-9",
                "display": "Summarization of episode note"}]
}


def persist_intake_document(tenant_id, subject_ref, pdf_bytes, *, title=None,
                             questionnaire_response_id=None):
    """Persist `pdf_bytes` as a FHIR DocumentReference under `tenant_id`.

    subject_ref: e.g. "Patient/123" — stored as DocumentReference.subject.
    title: attachment title; defaults to "Intake form".
    questionnaire_response_id: if provided, links the DocumentReference back
        to the QuestionnaireResponse the PDF was rendered from via both
        context.related and relatesTo, so the structured QR and the rendered
        PDF can be traced to each other.

    Returns the stored DocumentReference resource dict (with id/meta).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    doc = {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": _INTAKE_DOCUMENT_TYPE,
        "subject": {"reference": subject_ref},
        "date": now,
        "content": [{
            "attachment": {
                "contentType": "application/pdf",
                "title": title or "Intake form",
                "size": len(pdf_bytes),
                "data": base64.b64encode(pdf_bytes).decode("ascii"),
            }
        }],
    }
    if questionnaire_response_id:
        qr_reference = f"QuestionnaireResponse/{questionnaire_response_id}"
        doc["context"] = {"related": [{"reference": qr_reference}]}
        doc["relatesTo"] = [{"code": "transforms",
                             "target": {"reference": qr_reference}}]

    row = R6Resource(resource_type="DocumentReference",
                     resource_json=json.dumps(doc), tenant_id=tenant_id)
    db.session.add(row)
    db.session.commit()
    record_audit_event("create", "DocumentReference", row.id,
                       tenant_id=tenant_id, detail="intake pdf persisted")
    return row.to_fhir_json()


def get_document_pdf_bytes(tenant_id, docref_id):
    """Load the DocumentReference `docref_id` under `tenant_id` and decode
    its embedded PDF bytes back out of content[0].attachment.data.

    Returns None if the DocumentReference doesn't exist for that tenant, or
    has no embedded attachment data.
    """
    row = R6Resource.query.filter_by(resource_type="DocumentReference",
                                     id=docref_id, tenant_id=tenant_id).first()
    if row is None:
        return None
    resource = row.to_fhir_json()
    content = resource.get("content") or []
    if not content:
        return None
    data = (content[0].get("attachment") or {}).get("data")
    if not data:
        return None
    return base64.b64decode(data)
