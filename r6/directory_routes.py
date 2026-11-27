"""GET /r6/directory/search: find a clinician, practice or pharmacy.

A thin route over ``r6/directory.py`` for an agent tool. It needs a
tenant-bound step-up token, so each lookup is audited under a tenant, but the
tenant never leaves: the directory client is handed the search terms and
nothing else. Nothing limits lookups per tenant yet (#1192).

    ?kind=clinician&name=<name>&state=<two letters>
    ?kind=pharmacy&zip=<five digits>

The answer lists NPI, name, type, city, state and ZIP, and ``has_phone``. It
never carries a phone or fax number, and no route here serves one. Beside
the results it says how many rows the directory returned that are
not listed (``dropped``) and whether there may be more (``truncated``).

Audit: one AuditEvent per search that reached the client. Its detail is the
source and two counts, results and dropped. The search terms are not
recorded: next to a tenant id, a clinician's name says who somebody's doctor
is.

The answer is public directory data. No patient record is read to build it,
so there is nothing for redaction to strip and it is not passed through it.

OFF unless DIRECTORY_SEARCH_ENABLED is set (`search_enabled`): any tenant
grant could otherwise spend the directories' small anonymous quota, and
nothing limits searches per tenant yet. Off, the route answers 404 before the
tenant is read, so nothing is audited and nothing is sent. Turning it on is
the maintainer's call, per deployment, and waits for the per-tenant limit
(#1192) and the follow-ups in #1194.
"""

import os
from dataclasses import asdict

from flask import Blueprint, abort, jsonify, request

from models import db
from r6 import access, directory
from r6.runtime_config import TRUE_VALUES

directory_blueprint = Blueprint('directory', __name__,
                                url_prefix='/r6/directory')


def search_enabled():
    """Whether this engine answers the directory search. Read per request."""
    return os.environ.get('DIRECTORY_SEARCH_ENABLED', '').strip().lower() \
        in TRUE_VALUES


@directory_blueprint.route('/search', methods=['GET'])
def search():
    if not search_enabled():
        abort(404)
    tenant = access.tenant_from_request(sources=(access.TenantSource.HEADER,))
    access.require_grant(scope=access.Scope.TENANT_BOUND, tenant=tenant)

    kind = request.args.get('kind')
    # The terms are checked on their own, so a 400 can only mean the terms.
    # An error raised while a directory's answer is read is not one.
    try:
        if kind == 'clinician':
            source, find = 'ainpi', directory.find_clinicians
            terms = directory.clinician_terms(request.args.get('name'),
                                              request.args.get('state'))
        elif kind == 'pharmacy':
            source, find = 'nppes', directory.find_pharmacies
            terms = (directory.pharmacy_term(request.args.get('zip')),)
        else:
            raise ValueError('kind must be clinician or pharmacy')
    except ValueError:
        # Refused before anything was sent. Fixed wording: the terms are
        # not echoed back.
        return jsonify({'error': 'invalid_parameters'}), 400
    try:
        found = find(*terms)
    except directory.Unavailable:
        access.audit(tenant=tenant, event_type='read',
                     resource_type='Directory', outcome='failure',
                     detail=f'directory search: source={source}; unavailable')
        db.session.commit()
        return jsonify({'status': 'unavailable', 'results': []}), 503

    # The commit carries the audit row and nothing else: this GET is
    # declared for it in the write-guard matrix's KNOWN_GET_MUTATORS.
    access.audit(tenant=tenant, event_type='read', resource_type='Directory',
                 detail=f'directory search: source={source}; '
                        f'results={len(found.listings)}; '
                        f'dropped={found.dropped}')
    db.session.commit()
    return jsonify({'status': 'ok',
                    'results': [asdict(entry) for entry in found.listings],
                    'dropped': found.dropped, 'truncated': found.truncated})
