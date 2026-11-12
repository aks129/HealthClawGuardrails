"""
Attested facts: what a person has said about themselves for a call.

Their legal name, the name they want used on a call, date of birth, a
callback number and insurance details. The person types these on a
signed-in page and attests they are their own; nothing here is derived
from a record or supplied by an agent. That is what keeps another
person's identity off a call.

Written and read with the server-to-server secret and a tenant, nothing
else. A tenant token of any scope is refused on both, before anything is
read, stored or audited: an agent never sees these values, and never sets
them.

One row per tenant and key. Audit detail names the keys, never a value.
"""

import re
import unicodedata
from datetime import date

from flask import jsonify
from sqlalchemy.exc import IntegrityError

from models import db
from r6.access import audit
from r6.actions.contacts import (ACCOUNT_HOLDER, internal_only_preamble,
                                 valid_business_number)
from r6.actions.models import _utcnow
from r6.actions.routes import actions_blueprint
from r6.body_guard import json_body_within_depth

_NAME_PUNCTUATION = frozenset(" .,'-")
_NAME_MAX = 80
_ID = re.compile(r'[A-Za-z0-9-]{1,30}')
# An insurer's name as printed on a card.
_PAYER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'&()/-]{0,79}")
_EARLIEST_BIRTH_YEAR = 1900


def _is_name(value):
    """A name: letters and the marks that combine with them (Unicode
    categories L and M), with the punctuation a name carries. It starts
    with a letter. Checked on the composed (NFC) form, which is the form
    stored."""
    if not isinstance(value, str):
        return False
    value = unicodedata.normalize('NFC', value)
    return (0 < len(value) <= _NAME_MAX
            and unicodedata.category(value[0])[0] == 'L'
            and all(unicodedata.category(ch)[0] in 'LM'
                    or ch in _NAME_PUNCTUATION for ch in value))


def _is_birth_date(value):
    """A real calendar day, YYYY-MM-DD, not in the future."""
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return False
    # fromisoformat also reads week dates and the compact form; only the
    # canonical spelling is stored.
    return (day.isoformat() == value
            and _EARLIEST_BIRTH_YEAR <= day.year
            and day <= _utcnow().date())


def _is_payer(value):
    return isinstance(value, str) and _PAYER.fullmatch(value) is not None


def _is_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


#: Every fact a person can attest, and the rule its value must pass.
#: `preferred_name` is how the person asked to be referred to on a call.
FACT_RULES = {
    'legal_name': _is_name,
    'preferred_name': _is_name,
    'date_of_birth': _is_birth_date,
    'callback_phone': valid_business_number,
    'payer_name': _is_payer,
    'member_id': _is_id,
    'group_number': _is_id,
}


class DelegationFact(db.Model):
    """One attested fact. One row per tenant and key; purged with the
    tenant."""
    __tablename__ = 'delegation_facts'

    tenant_id = db.Column(db.String(64), primary_key=True)
    key = db.Column(db.String(32), primary_key=True)
    value = db.Column(db.String(120), nullable=False)
    attested_at = db.Column(db.DateTime, nullable=False, default=_utcnow)


def _tenant_facts(tenant_id):
    return {fact.key: fact for fact in
            DelegationFact.query.filter_by(tenant_id=tenant_id).all()}


def _form_view(facts):
    return {'facts': {key: {'value': fact.value,
                            'attested_at': fact.attested_at.isoformat()}
                      for key, fact in sorted(facts.items())}}


def _facts_or_errors(body, stored_keys):
    """(facts, []) for a body that passes every rule, else (None, names).

    The errors are names from this module only: a fact key, `facts` or
    `attested`. A refused value, or a key nobody defined, is never echoed.
    """
    facts = body.get('facts')
    if not isinstance(facts, dict) or not facts:
        return None, ['facts']
    if not set(facts) <= set(FACT_RULES):
        return None, ['facts']
    failed = sorted(key for key, value in facts.items()
                    if not FACT_RULES[key](value))
    # A call opens with the name the person chose, so a legal name is never
    # held without one.
    held = stored_keys | set(facts)
    if ('legal_name' in held and 'preferred_name' not in held
            and 'preferred_name' not in failed):
        failed.append('preferred_name')
    # Ticked by the person on the form. Only the JSON literal true counts.
    if body.get('attested') is not True:
        failed.append('attested')
    if failed:
        return None, failed
    return facts, []


@actions_blueprint.route('/facts', methods=['PUT'])
def attest_facts():
    """Store the facts a person typed and attested on a signed-in page."""
    tenant, refusal = internal_only_preamble()
    if refusal is not None:
        return refusal
    body, too_deep = json_body_within_depth()
    if too_deep or not isinstance(body, dict):
        return jsonify({'error': 'A JSON object is required'}), 400
    stored = _tenant_facts(tenant.id)
    facts, failed = _facts_or_errors(body, set(stored))
    if failed:
        return jsonify({'error': 'The details were not saved.',
                        'errors': failed}), 422
    now = _utcnow()
    for key, value in facts.items():
        fact = stored.get(key)
        if fact is None:
            fact = DelegationFact(tenant_id=tenant.id, key=key)
            db.session.add(fact)
            stored[key] = fact
        # Stored composed, so one name has one spelling.
        fact.value = unicodedata.normalize('NFC', value)
        fact.attested_at = now
    try:
        db.session.flush()
    except IntegrityError:
        # Another request stored the same new key first.
        db.session.rollback()
        return jsonify({'error': 'These details were being saved by '
                                 'another request. Send them again.'}), 409
    audit(tenant=tenant, event_type='update',
          resource_type='DelegationFact', agent_id=ACCOUNT_HOLDER,
          detail='facts attested: %s' % ','.join(sorted(facts)))
    db.session.commit()
    return jsonify(_form_view(stored))


@actions_blueprint.route('/facts', methods=['GET'])
def read_facts():
    """The person's facts, to prefill their own form. Secret only."""
    tenant, refusal = internal_only_preamble()
    if refusal is not None:
        return refusal
    stored = _tenant_facts(tenant.id)
    # The read's own row, committed here: the only thing this GET writes.
    audit(tenant=tenant, event_type='read',
          resource_type='DelegationFact', agent_id=ACCOUNT_HOLDER,
          detail='facts read; count=%d' % len(stored))
    db.session.commit()
    return jsonify(_form_view(stored))
