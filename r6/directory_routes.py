"""GET /r6/directory/search: find a clinician, practice or pharmacy.

A thin route over ``r6/directory.py`` for an agent tool. It needs a
tenant-bound step-up token, so the lookup is rate-attributable and audited,
but the tenant never leaves: the directory client is handed the search terms
and nothing else.

    ?kind=clinician&name=<name>&state=<two letters>
    ?kind=pharmacy&zip=<five digits>

The answer lists NPI, name, type, city, state and ZIP, and ``has_phone``. It
never carries a phone or fax number. Those come from
``r6.directory.contact`` for one chosen NPI, which no route here serves.

Audit: one AuditEvent per search that reached the client. Its detail is the
source and the result count. The search terms are not recorded: next to a
tenant id, a clinician's name says who somebody's doctor is.
"""

from dataclasses import asdict

from flask import Blueprint, jsonify, request

from models import db
from r6 import access, directory

directory_blueprint = Blueprint('directory', __name__,
                                url_prefix='/r6/directory')


@directory_blueprint.route('/search', methods=['GET'])
def search():
    tenant = access.tenant_from_request(sources=(access.TenantSource.HEADER,))
    access.require_grant(scope=access.Scope.TENANT_BOUND, tenant=tenant)

    kind = request.args.get('kind')
    try:
        if kind == 'clinician':
            source = 'ainpi'
            found = directory.find_clinicians(request.args.get('name'),
                                              request.args.get('state'))
        elif kind == 'pharmacy':
            source = 'nppes'
            found = directory.find_pharmacies(request.args.get('zip'))
        else:
            raise ValueError('kind must be clinician or pharmacy')
    except ValueError:
        # Refused before anything was sent. Fixed wording: the terms are
        # not echoed back.
        return jsonify({'error': 'invalid_parameters'}), 400
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
                        f'results={len(found)}')
    db.session.commit()
    return jsonify({'status': 'ok',
                    'results': [asdict(entry) for entry in found]})
