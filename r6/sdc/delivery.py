"""Signed, expiring download links for a persisted intake-PDF DocumentReference.

A patient (or a clinic) opens the link and downloads the PDF with no login and
no headers: the HMAC signature embedded in the URL *is* the authorization. The
signing reuses the same HMAC-SHA256 / STEP_UP_SECRET pattern as r6/stepup.py —
no new crypto is invented here.

The public download route lives on its OWN blueprint (sdc_delivery_blueprint),
NOT on r6_blueprint, precisely because r6_blueprint's before_request hook
enforces X-Tenant-Id / X-Step-Up-Token. This route must be reachable without
those headers.

Future enhancement: wrap the link in a full SMART Health Link (SHL) envelope —
encrypted manifest + one-time flag — which lives in the Node server. That is
out of scope for this signed-download-route implementation.
"""

import hashlib
import hmac
import os
import time
from urllib.parse import quote

from flask import Blueprint, Response, request

from r6.sdc.documents import get_document_pdf_bytes
from r6.audit import record_audit_event

# One week — a patient/clinic link that is emailed and opened days later.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # 604800


def _link_secret():
    """Shared HMAC secret — same source as the step-up tokens."""
    return os.environ.get('STEP_UP_SECRET', '')


def _sign(tenant_id, docref_id, exp):
    """Hex HMAC-SHA256 over the exact message ``tenant_id\\ndocref_id\\nexp``.

    Raises ValueError (fail loud) when STEP_UP_SECRET is unset — an unsigned or
    empty-key signature must never be handed out.
    """
    secret = _link_secret()
    if not secret:
        raise ValueError('STEP_UP_SECRET is required to sign a document link')
    msg = f"{tenant_id}\n{docref_id}\n{exp}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def build_document_link(tenant_id, docref_id, *, ttl_seconds=DEFAULT_TTL_SECONDS,
                        now=None):
    """Build an ABSOLUTE, signed, expiring download URL for a DocumentReference.

    base comes from PUBLIC_BASE_URL. Raises ValueError (fail loud, mirroring
    r6/actions/rails/form_fill.py) when PUBLIC_BASE_URL is unset — a delivery
    link with no host is useless and must never be silently produced.

    ``now`` is injectable for deterministic tests; defaults to time.time().
    """
    base = os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')
    if not base:
        raise ValueError('PUBLIC_BASE_URL is required to build a delivery link')
    exp = int(now or time.time()) + ttl_seconds
    sig = _sign(tenant_id, docref_id, exp)
    return (f"{base}/r6/sdc/documents/{docref_id}"
            f"?t={quote(tenant_id)}&exp={exp}&sig={sig}")


def verify_document_link(tenant_id, docref_id, exp, sig, *, now=None):
    """Verify a signed download link. Returns ``(ok, reason)``.

    Order matters: the signature is checked BEFORE expiry, so a tampered exp is
    reported as 'bad-signature' (not 'expired'). reason is one of
    'malformed', 'bad-signature', 'expired', or 'ok'.
    """
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False, 'malformed'
    expected = _sign(tenant_id, docref_id, exp_int)
    if not hmac.compare_digest(expected, sig):
        return False, 'bad-signature'
    if int(now or time.time()) > exp_int:
        return False, 'expired'
    return True, 'ok'


# ---------------------------------------------------------------------------
# Public download route — deliberately NOT on r6_blueprint (whose
# before_request hook enforces tenant headers). The signature is the credential.
# ---------------------------------------------------------------------------
sdc_delivery_blueprint = Blueprint('sdc_delivery', __name__, url_prefix='/r6/sdc')


@sdc_delivery_blueprint.route('/documents/<docref_id>', methods=['GET'])
def download_document(docref_id):
    """Serve the intake PDF for ``docref_id`` when the signed link verifies."""
    tenant_id = request.args.get('t')
    exp = request.args.get('exp')
    sig = request.args.get('sig')
    if not tenant_id or not exp or not sig:
        return Response('missing link parameters', status=400,
                        mimetype='text/plain')

    ok, reason = verify_document_link(tenant_id, docref_id, exp, sig)
    if not ok:
        if reason == 'expired':
            return Response('link expired', status=410, mimetype='text/plain')
        # bad-signature / malformed
        return Response('invalid link', status=403, mimetype='text/plain')

    pdf = get_document_pdf_bytes(tenant_id, docref_id)
    if pdf is None:
        return Response('document not found', status=404, mimetype='text/plain')

    record_audit_event('read', resource_type='DocumentReference',
                       resource_id=docref_id, tenant_id=tenant_id,
                       detail='intake pdf downloaded via signed link')

    resp = Response(pdf, mimetype='application/pdf')
    resp.headers['Content-Disposition'] = 'attachment; filename="intake.pdf"'
    return resp
