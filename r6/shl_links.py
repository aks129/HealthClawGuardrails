"""The SMART Health Link store: ciphertext only, short-lived, limited use.

The ktc export (r6/ktc.py) with `"deliver": "link"` encrypts the profiled
bundle under a fresh key (r6/shl.py), stores the ciphertext here and
answers the shlink. The key goes back to the caller inside the shlink and
is never stored, logged or audited, so a row here cannot be read without
the link that names it.

A receiver fetches the file with GET /shl/<link_id> (the U flag: no
manifest, no passcode). It is served while the link is unexpired and has a
use left; each fetch spends one, the last one also drops the ciphertext,
and anything else answers 404, as the spec asks of an inactive link. The
`recipient` query parameter is accepted and ignored: it is the receiver's
own text and never reaches a log or the audit trail.

The route is on its own blueprint, not r6_blueprint, because the receiver
sends no tenant or token header: the link id is the credential, as the
signed URL is in r6/sdc/delivery.py. Only its SHA-256 is stored, so the
table does not hold working URLs either.

Settings (engine environment):
- PUBLIC_BASE_URL: the engine's public https origin. The file URL is built
  on it and must stay within the spec's 128 characters. Unset, no link is
  issued.
- SHL_LINK_TTL_SECONDS: lifetime, default 3600, clamped to 60..86400.
- SHL_LINK_MAX_USES: fetches per link, default 1, clamped to 1..5.
"""

import hashlib
import os
import re
import time
from datetime import datetime, timezone

from flask import Blueprint, Response
from sqlalchemy import update

from models import db
from r6 import shl
from r6.audit import add_audit_event

DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_USES = 1

#: Fixed words, audited as the agent; nothing a receiver sends reaches a row.
AUDIT_AGENT = 'careagents-ktc'

#: A link id is 32 random bytes, base64url: 43 characters.
_LINK_ID = re.compile(r'[A-Za-z0-9_-]{43}')


class ShlLink(db.Model):
    """One stored SHL file. No key, no plaintext, no patient id."""
    __tablename__ = 'shl_links'

    id_hash = db.Column(db.String(64), primary_key=True)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    ciphertext = db.Column(db.Text, nullable=True)
    exp = db.Column(db.BigInteger, nullable=False, index=True)
    uses_left = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False,
                           default=lambda: datetime.now(timezone.utc))


def _int_env(name, default, low, high):
    try:
        value = int(os.environ.get(name, '') or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


def ttl_seconds():
    return _int_env('SHL_LINK_TTL_SECONDS', DEFAULT_TTL_SECONDS, 60, 86400)


def max_uses():
    return _int_env('SHL_LINK_MAX_USES', DEFAULT_MAX_USES, 1, 5)


def public_base():
    """The engine's public https origin, or None when it is unset or not
    https (a receiver fetches only https)."""
    base = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')
    return base if base.startswith('https://') else None


def _hash(link_id):
    return hashlib.sha256(link_id.encode('ascii')).hexdigest()


def issue(tenant_id, plaintext, label, now=None):
    """Encrypt `plaintext` (bytes), store the ciphertext, and return
    {link_id, shlink, exp}. Adds to the caller's transaction; the caller
    commits. Raises ValueError when PUBLIC_BASE_URL is not usable."""
    base = public_base()
    if base is None:
        raise ValueError('PUBLIC_BASE_URL must be an https origin')
    now = int(time.time() if now is None else now)
    # Expired rows go first: the store holds only links that can still work.
    ShlLink.query.filter(ShlLink.exp <= now).delete(synchronize_session=False)
    link_id = shl.b64url(os.urandom(32))
    key = shl.new_key()
    exp = now + ttl_seconds()
    shlink = shl.build_shlink(f'{base}/shl/{link_id}', key, exp, label)
    db.session.add(ShlLink(id_hash=_hash(link_id), tenant_id=tenant_id,
                           ciphertext=shl.encrypt(key, plaintext), exp=exp,
                           uses_left=max_uses()))
    return {'link_id': link_id, 'shlink': shlink, 'exp': exp}


shl_blueprint = Blueprint('shl', __name__, url_prefix='/shl')


def _gone():
    resp = Response('not found', status=404, mimetype='text/plain')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@shl_blueprint.route('/<link_id>', methods=['GET'])
def fetch_file(link_id):
    """The U-flag file: the JWE while the link lives, else 404."""
    if not _LINK_ID.fullmatch(link_id or ''):
        return _gone()
    id_hash = _hash(link_id)
    now = int(time.time())
    row = db.session.get(ShlLink, id_hash)
    if row is None or not row.ciphertext:
        return _gone()
    jwe = row.ciphertext
    # One use, spent atomically: of two racing fetches of the last use,
    # exactly one is served.
    spent = db.session.execute(
        update(ShlLink)
        .where(ShlLink.id_hash == id_hash, ShlLink.uses_left > 0,
               ShlLink.exp > now)
        .values(uses_left=ShlLink.uses_left - 1))
    if spent.rowcount != 1:
        db.session.rollback()
        return _gone()
    db.session.execute(
        update(ShlLink)
        .where(ShlLink.id_hash == id_hash, ShlLink.uses_left <= 0)
        .values(ciphertext=None))
    add_audit_event(
        'read', resource_type='Bundle', resource_id='shl-file',
        agent_id=AUDIT_AGENT, tenant_id=row.tenant_id,
        detail='shl file served to a link holder (one use spent)')
    db.session.commit()
    resp = Response(jwe, status=200, mimetype='application/jose')
    resp.headers['Cache-Control'] = 'no-store'
    return resp
