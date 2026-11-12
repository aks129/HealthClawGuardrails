"""
Saved contacts: the business numbers a person has attested may be called.

A contact is where a call goes, so the write decides who chooses the
destination. It takes the server-to-server secret and a tenant, and nothing
else: an approval surface calls it from a signed-in page, after the person
typed the number and ticked that it is a business line. A tenant write
token is refused, because an agent holds one: the party asking for a call
must not also be the one that says where calls may go.

Two reads. A tenant-bound token lists id, label, kind and status, never a
number. The secret lists everything, to prefill the person's own form.

A label and a number say which pharmacy or dentist a person uses, so they
stay out of audit detail: a row carries the contact id and the kind.
"""

import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import jsonify, request

from models import db
from r6.access import Scope, audit, require_grant
from r6.actions.models import _utcnow
from r6.actions.routes import _error, _tenant_or_none, actions_blueprint
from r6.body_guard import json_body_within_depth
from r6.internal_auth import internal_secret_presented

CALLEE_KINDS = ('clinic', 'dentist', 'pharmacy', 'other')
STATUSES = ('active', 'suspended')

#: Audit agent for a write made on the person's behalf by a signed-in page.
ACCOUNT_HOLDER = 'account-holder'

# The label is spoken on a call, so it is letters, digits, spaces and the
# punctuation a business name carries; nothing a renderer or a voice reads
# as markup.
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'&()/#-]{0,59}")
# +1 numbers only: then an area code and an exchange that
# each start 2-9. ASCII digits only: \d would also take other scripts'
# digits, which the service-code rules below would not recognize.
_NANP = re.compile(r'\+1([2-9][0-9]{2})([2-9][0-9]{2})[0-9]{4}')
_PREMIUM_AREA_CODES = frozenset({'900'})
_PREMIUM_EXCHANGES = frozenset({'976'})


def _is_n11(three_digits):
    """211 through 911: service codes, never a business line."""
    return three_digits[1:] == '11'


def valid_business_number(value):
    """True for a +1 number that can be a business line.

    +1 numbers only, which is the whole North American plan: the United
    States, Canada and the Caribbean countries that share it. Refuses the
    N11 service codes (911 and the rest) as an area code or an exchange,
    and premium-rate 900 and 976. Toll-free numbers pass: pharmacies use
    them. This is a short deny-list, not a directory: it does not screen
    other premium ranges or say what kind of line a number is.
    """
    if not isinstance(value, str):
        return False
    match = _NANP.fullmatch(value)
    if match is None:
        return False
    area, exchange = match.groups()
    if _is_n11(area) or _is_n11(exchange):
        return False
    return (area not in _PREMIUM_AREA_CODES
            and exchange not in _PREMIUM_EXCHANGES)


def _valid_tz(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


class DelegationContact(db.Model):
    """One saved contact. Tenant-scoped; purged with the tenant."""
    __tablename__ = 'delegation_contacts'

    id = db.Column(db.String(64), primary_key=True,
                   default=lambda: 'c_' + uuid.uuid4().hex)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    label = db.Column(db.String(60), nullable=False)
    phone_e164 = db.Column(db.String(16), nullable=False)
    callee_kind = db.Column(db.String(16), nullable=False)
    tz = db.Column(db.String(64), nullable=False)
    attested_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    status = db.Column(db.String(16), nullable=False, default='active')
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow,
                           onupdate=_utcnow)

    def agent_view(self):
        """What a tenant-bound token may read. No number."""
        return {'id': self.id, 'label': self.label,
                'callee_kind': self.callee_kind, 'status': self.status}

    def form_view(self):
        """What the person's own form is prefilled with."""
        return dict(self.agent_view(), phone_e164=self.phone_e164,
                    tz=self.tz, attested_at=self.attested_at.isoformat())


def _fields_or_errors(body):
    """(fields, []) for a body that passes every rule, else (None, names).

    The errors are field names only. A refused value is never echoed.
    """
    label = body.get('label')
    label = label.strip() if isinstance(label, str) else ''
    checks = (
        ('label', _LABEL.fullmatch(label) is not None),
        ('phone_e164', valid_business_number(body.get('phone_e164'))),
        ('callee_kind', body.get('callee_kind') in CALLEE_KINDS),
        ('tz', _valid_tz(body.get('tz'))),
        # Ticked by the person on the form. Only the JSON literal true
        # counts.
        ('business_line', body.get('business_line') is True),
    )
    failed = [name for name, ok in checks if not ok]
    if failed:
        return None, failed
    return {'label': label, 'phone_e164': body['phone_e164'],
            'callee_kind': body['callee_kind'], 'tz': body['tz']}, []


def _write_preamble():
    """(tenant, None) for an authorized write, else (None, refusal).

    The secret is checked before anything is read, stored or audited, so a
    caller without it leaves no trace of the contact it tried to save.
    """
    tenant = _tenant_or_none()
    if tenant is None:
        return None, _error(400, 'X-Tenant-Id header is required')
    if not internal_secret_presented():
        return None, _error(403, 'Forbidden')
    return tenant, None


def _validated_fields():
    """(fields, None) for a valid body, else (None, refusal)."""
    body, too_deep = json_body_within_depth()
    if too_deep or not isinstance(body, dict):
        return None, _error(400, 'A JSON object is required')
    fields, failed = _fields_or_errors(body)
    if failed:
        return None, (jsonify({'error': 'The contact was not saved.',
                               'errors': failed}), 422)
    return fields, None


def _audit_saved(contact, event_type):
    audit(tenant=contact.tenant_id, event_type=event_type,
          resource_type='DelegationContact', resource_id=contact.id,
          agent_id=ACCOUNT_HOLDER,
          detail='contact saved; contact=%s; kind=%s'
                 % (contact.id, contact.callee_kind))


@actions_blueprint.route('/contacts', methods=['POST'])
def save_contact():
    """Save a contact the person typed and attested on a signed-in page."""
    tenant, refusal = _write_preamble()
    if refusal is not None:
        return refusal
    fields, refusal = _validated_fields()
    if refusal is not None:
        return refusal
    contact = DelegationContact(tenant_id=tenant.id, **fields)
    db.session.add(contact)
    db.session.flush()
    _audit_saved(contact, 'create')
    db.session.commit()
    return jsonify(contact.form_view()), 201


@actions_blueprint.route('/contacts/<contact_id>', methods=['PUT'])
def resave_contact(contact_id):
    """Save a contact again. A fresh attestation, so it is active again."""
    tenant, refusal = _write_preamble()
    if refusal is not None:
        return refusal
    contact = DelegationContact.query.filter_by(
        id=contact_id, tenant_id=tenant.id).first()
    if contact is None:
        return _error(404, 'Unknown contact')
    fields, refusal = _validated_fields()
    if refusal is not None:
        return refusal
    for name, value in fields.items():
        setattr(contact, name, value)
    contact.status = 'active'
    contact.attested_at = _utcnow()
    db.session.flush()
    _audit_saved(contact, 'update')
    db.session.commit()
    return jsonify(contact.form_view())


@actions_blueprint.route('/contacts', methods=['GET'])
def list_contacts():
    """The tenant's contacts: without numbers for a tenant-bound token,
    with them for the secret that serves the person's own form."""
    tenant = _tenant_or_none()
    if tenant is None:
        return _error(400, 'X-Tenant-Id header is required')
    for_form = internal_secret_presented()
    if not for_form:
        require_grant(scope=Scope.TENANT_BOUND, tenant=tenant,
                      also_bearer=True)
    contacts = DelegationContact.query.filter_by(
        tenant_id=tenant.id).order_by(DelegationContact.created_at,
                                      DelegationContact.id).all()
    # The read's own row, committed here: the only thing this GET writes.
    audit(tenant=tenant, event_type='read',
          resource_type='DelegationContact',
          agent_id=(ACCOUNT_HOLDER if for_form
                    else request.headers.get('X-Agent-Id')),
          detail='contacts listed; count=%d; numbers=%s'
                 % (len(contacts), 'yes' if for_form else 'no'))
    db.session.commit()
    view = (DelegationContact.form_view if for_form
            else DelegationContact.agent_view)
    return jsonify({'contacts': [view(c) for c in contacts]})


def suspend_contact(tenant_id, contact_id):
    """Suspend a contact until the person saves it again. True if it was
    this tenant's and active. Audited in the caller's transaction; the
    caller commits."""
    contact = DelegationContact.query.filter_by(
        id=contact_id, tenant_id=tenant_id).first()
    if contact is None or contact.status != 'active':
        return False
    contact.status = 'suspended'
    db.session.flush()
    audit(tenant=tenant_id, event_type='update',
          resource_type='DelegationContact', resource_id=contact.id,
          detail='contact suspended; contact=%s' % contact.id)
    return True
