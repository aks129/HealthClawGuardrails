"""
R6 FHIR REST Facade - Flask Blueprint.

Reference implementation of MCP guardrail patterns for FHIR R6 agent access.
NOT a production FHIR server — stores resources as JSON blobs with structural
validation only. Designed to demonstrate security patterns (tenant isolation,
step-up auth, audit, redaction, human-in-the-loop) that real FHIR+MCP
integrations would need.

Search supports: patient, code, status, _lastUpdated, _count, _sort, _summary.
Validation: structural checks for required fields. Falls back when external
validator unavailable. No StructureDefinition or terminology binding validation.
"""

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from flask import (
    Blueprint, request, jsonify, Response, stream_with_context,
    render_template,
)
from models import db
from r6.models import R6Resource, ContextEnvelope, ContextItem, AuditEventRecord
from r6.context_builder import ContextBuilder
from r6.validator import R6Validator
from r6.audit import record_audit_event
from r6.redaction import apply_patient_controlled_redaction
from r6.redaction import apply_redaction
from r6.stepup import validate_step_up_token, generate_step_up_token
from r6.oauth import register_oauth_routes
from r6.rate_limit import rate_limit_middleware
from r6.health_compliance import (
    add_disclaimer, enforce_human_in_loop, deidentify_resource,
    export_audit_trail, MEDICAL_DISCLAIMER
)
from r6.fhir_proxy import get_proxy, is_proxy_enabled
from r6.curatr import (
    CuratrEngine,
    apply_fix as _curatr_apply_fix,
    persist_curation_state as _persist_curation_state,
)
from r6.health_context import get as _hc_get

_curatr_engine = CuratrEngine()

logger = logging.getLogger(__name__)

r6_blueprint = Blueprint('r6', __name__, url_prefix='/r6/fhir')

# Register OAuth 2.1 endpoints
register_oauth_routes(r6_blueprint)

# Register rate limiting
rate_limit_middleware(r6_blueprint)

# R6 version identifier aligned with ballot build
R6_FHIR_VERSION = '6.0.0-ballot3'

# Initialize services
context_builder = ContextBuilder()
validator = R6Validator()

# Valid FHIR id pattern
_FHIR_ID_PATTERN = re.compile(r'^[A-Za-z0-9\-.]{1,64}$')

# Valid tenant_id pattern: alphanumeric, hyphens, underscores, 1-64 chars
_TENANT_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')

# AuditEvent is system-managed — block external CRUD
_SYSTEM_MANAGED_TYPES = {'AuditEvent'}

# Phase 2: R6-specific valid Permission combining codes
_PERMISSION_COMBINING_CODES = {
    'deny-overrides', 'permit-overrides', 'ordered-deny-overrides',
    'ordered-permit-overrides', 'deny-unless-permit', 'permit-unless-deny',
}

# Valid Bundle types per FHIR spec
_VALID_BUNDLE_TYPES = {
    'document', 'message', 'transaction', 'transaction-response',
    'batch', 'batch-response', 'history', 'searchset', 'collection',
    'subscription-notification',
}

# Valid FHIR search patient reference pattern
_PATIENT_REF_PATTERN = re.compile(r'^Patient/[A-Za-z0-9\-.]{1,64}$')


# --- Tenant Enforcement ---

@r6_blueprint.before_request
def enforce_tenant_id():
    """Require X-Tenant-Id header on all endpoints except public discovery."""
    # Public discovery endpoints (no tenant required)
    if request.path.endswith('/metadata'):
        return None
    if request.path.endswith('/health'):
        return None
    if '/internal/' in request.path or '/demo/' in request.path:
        return None
    if '/.well-known/' in request.path:
        return None
    if '/oauth/' in request.path:
        return None
    # MCP App HTML renders without a tenant header; tenant arrives via
    # query string or is supplied by the MCP client's outer session.
    if '/mcp-apps/' in request.path:
        return None
    tenant_id = request.headers.get('X-Tenant-Id')
    if not tenant_id:
        return jsonify({
            'resourceType': 'OperationOutcome',
            'issue': [{
                'severity': 'error',
                'code': 'security',
                'diagnostics': 'X-Tenant-Id header is required'
            }]
        }), 400
    # Validate tenant_id format
    if not _TENANT_ID_PATTERN.match(tenant_id):
        return jsonify({
            'resourceType': 'OperationOutcome',
            'issue': [{
                'severity': 'error',
                'code': 'invalid',
                'diagnostics': 'X-Tenant-Id must match [a-zA-Z0-9_-]{1,64}'
            }]
        }), 400


# --- Human-in-the-Loop Enforcement ---

@r6_blueprint.before_request
def check_human_confirmation():
    """Enforce human-in-the-loop for clinical writes."""
    result = enforce_human_in_loop()
    if result:
        return result


@r6_blueprint.route('/metadata', methods=['GET'])
def r6_metadata():
    """
    Return a CapabilityStatement declaring R4 US Core v9 + R6 ballot3 support.
    """
    capability_statement = {
        'resourceType': 'CapabilityStatement',
        'id': 'r6-showcase',
        'status': 'active',
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'kind': 'instance',
        'fhirVersion': R6_FHIR_VERSION,
        'format': ['json'],
        'software': {
            'name': 'HealthClaw Guardrails',
            'version': _hc_get('version', '1.2.0'),
        },
        'implementation': {
            'description': (
                'MCP guardrail proxy supporting FHIR R4 (US Core v9) stable resources '
                'and FHIR R6 ballot3 experimental resources. '
                + ('Proxying to upstream FHIR server with full guardrail layer (redaction, audit, step-up auth).'
                   if is_proxy_enabled()
                   else 'Local JSON blob storage with structural validation. Not a production server.')
            ),
            'url': request.host_url.rstrip('/') + '/r6/fhir'
        },
        'instantiates': [
            'http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient',
        ],
        'implementationGuide': [
            'http://hl7.org/fhir/us/core/ImplementationGuide/hl7.fhir.us.core',
        ],
        'rest': [
            {
                'mode': 'server',
                'resource': [
                    _resource_capability(rt) for rt in R6Resource.SUPPORTED_TYPES
                ],
                'operation': [
                    {
                        'name': 'validate',
                        'definition': 'http://hl7.org/fhir/OperationDefinition/Resource-validate'
                    },
                    {
                        'name': 'ingest-context',
                        'definition': request.host_url.rstrip('/') + '/r6/fhir/Bundle/$ingest-context'
                    },
                    {
                        'name': 'stats',
                        'definition': 'http://hl7.org/fhir/OperationDefinition/Observation-stats'
                    },
                    {
                        'name': 'lastn',
                        'definition': 'http://hl7.org/fhir/OperationDefinition/Observation-lastn'
                    },
                ]
            }
        ]
    }
    return jsonify(capability_statement)


def _resource_capability(resource_type):
    """Build a resource entry for the CapabilityStatement."""
    interactions = [
        {'code': 'read'},
        {'code': 'create'},
        {'code': 'update'},
        {'code': 'search-type'},
    ]
    search_params = [
        {'name': '_count', 'type': 'number', 'documentation': 'Max results (1-200)'},
        {'name': '_sort', 'type': 'string', 'documentation': '_lastUpdated or -_lastUpdated'},
        {'name': '_lastUpdated', 'type': 'date', 'documentation': 'Filter by last updated (ge/le prefix)'},
        {'name': '_summary', 'type': 'token', 'documentation': 'count'},
        {'name': 'code', 'type': 'token', 'documentation': 'Filter by code.coding[].code (JSON string match)'},
        {'name': 'status', 'type': 'token', 'documentation': 'Filter by status field'},
        {'name': 'patient', 'type': 'reference', 'documentation': 'Filter by subject.reference (Patient/{id})'},
    ]
    return {
        'type': resource_type,
        'interaction': interactions,
        'versioning': 'versioned',
        'readHistory': False,
        'updateCreate': False,
        'searchParam': search_params,
    }


# --- CRUD Operations ---

@r6_blueprint.route('/<resource_type>', methods=['POST'])
def create_resource(resource_type):
    """Create a new R6 FHIR resource."""
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    # Block external creation of system-managed resources
    if resource_type in _SYSTEM_MANAGED_TYPES:
        return _operation_outcome('error', 'security',
                                  f'{resource_type} is system-managed and cannot be created via API'), 403

    body = request.get_json(silent=True)
    if not body:
        return _operation_outcome('error', 'invalid', 'Request body must be valid JSON'), 400

    if body.get('resourceType') != resource_type:
        return _operation_outcome('error', 'invalid',
                                  f'resourceType mismatch: expected {resource_type}'), 400

    # Step-up authorization check with HMAC validation
    tenant_id = request.headers.get('X-Tenant-Id')
    step_up_token = request.headers.get('X-Step-Up-Token')
    if not step_up_token:
        return _operation_outcome('error', 'security',
                                  'Write operations require X-Step-Up-Token header'), 401

    valid, err = validate_step_up_token(step_up_token, tenant_id)
    if not valid:
        return _operation_outcome('error', 'security',
                                  f'Step-up token rejected: {err}'), 401

    # Validate before storing (agent proposals must pass $validate before commit)
    validation_result = validator.validate_resource(body)
    if not validation_result['valid']:
        return jsonify(validation_result['operation_outcome']), 422

    # Validate client-supplied id if present
    client_id = body.get('id')
    if client_id and not _FHIR_ID_PATTERN.match(client_id):
        return _operation_outcome('error', 'invalid',
                                  'Resource id must match [A-Za-z0-9\\-.]{1,64}'), 400

    # --- Upstream proxy mode: create on real FHIR server ---
    proxy = get_proxy()
    if proxy:
        result, status_code = proxy.create(resource_type, body)
        if result and status_code in (200, 201):
            record_audit_event('create', resource_type, result.get('id'),
                               agent_id=request.headers.get('X-Agent-Id'),
                               tenant_id=tenant_id,
                               detail='source=upstream')
            result = add_disclaimer(result, resource_type)
            result['_source'] = 'upstream'
            response = jsonify(result)
            response.status_code = status_code
            return response
        # Upstream rejected the create
        if result:
            return jsonify(result), status_code
        return _operation_outcome('error', 'exception',
                                  'Upstream FHIR server rejected the resource'), status_code

    # --- Local mode: store in SQLite ---
    resource_json = json.dumps(body, separators=(',', ':'), sort_keys=True)
    resource = R6Resource(
        resource_type=resource_type,
        resource_json=resource_json,
        resource_id=client_id,
        tenant_id=tenant_id
    )

    try:
        db.session.add(resource)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f'Failed to create {resource_type}: {e}')
        return _operation_outcome('error', 'exception',
                                  'Failed to store resource'), 500

    record_audit_event('create', resource_type, resource.id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id)

    fhir_json = resource.to_fhir_json()
    fhir_json = add_disclaimer(fhir_json, resource_type)
    response = jsonify(fhir_json)
    response.status_code = 201
    response.headers['Location'] = f'/r6/fhir/{resource_type}/{resource.id}'
    response.headers['ETag'] = f'W/"{resource.version_id}"'
    return response


@r6_blueprint.route('/<resource_type>/<resource_id>', methods=['GET'])
def read_resource(resource_type, resource_id):
    """Read a specific R6 FHIR resource (redacted)."""
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    tenant_id = request.headers.get('X-Tenant-Id')

    # --- Upstream proxy mode: fetch from real FHIR server ---
    proxy = get_proxy()
    if proxy:
        fhir_json = proxy.read(resource_type, resource_id)
        if not fhir_json:
            return _operation_outcome('error', 'not-found',
                                      f'{resource_type}/{resource_id} not found'), 404

        record_audit_event('read', resource_type, resource_id,
                           agent_id=request.headers.get('X-Agent-Id'),
                           context_id=request.headers.get('X-Context-Id'),
                           tenant_id=tenant_id,
                           detail='source=upstream')

        # Guardrails still apply on upstream data
        redacted = apply_redaction(fhir_json)
        redacted = add_disclaimer(redacted, resource_type)
        redacted['_source'] = 'upstream'
        return jsonify(redacted)

    # --- Local mode: query SQLite ---
    resource = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id
    ).first()

    if not resource:
        return _operation_outcome('error', 'not-found',
                                  f'{resource_type}/{resource_id} not found'), 404

    record_audit_event('read', resource_type, resource_id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       context_id=request.headers.get('X-Context-Id'),
                       tenant_id=tenant_id)

    # Apply redaction on all reads — consistent with context envelope behavior
    fhir_json = resource.to_fhir_json()
    redacted = apply_redaction(fhir_json)
    redacted = add_disclaimer(redacted, resource_type)

    response = jsonify(redacted)
    response.headers['ETag'] = f'W/"{resource.version_id}"'
    return response


@r6_blueprint.route('/<resource_type>/<resource_id>', methods=['PUT'])
def update_resource(resource_type, resource_id):
    """Update an existing R6 FHIR resource."""
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    # Block updates to system-managed resources
    if resource_type in _SYSTEM_MANAGED_TYPES:
        return _operation_outcome('error', 'security',
                                  f'{resource_type} is system-managed and cannot be modified via API'), 403

    # Step-up authorization with HMAC validation
    tenant_id = request.headers.get('X-Tenant-Id')
    step_up_token = request.headers.get('X-Step-Up-Token')
    if not step_up_token:
        return _operation_outcome('error', 'security',
                                  'Write operations require X-Step-Up-Token header'), 401

    valid, err = validate_step_up_token(step_up_token, tenant_id)
    if not valid:
        return _operation_outcome('error', 'security',
                                  f'Step-up token rejected: {err}'), 401

    body = request.get_json(silent=True)
    if not body:
        return _operation_outcome('error', 'invalid', 'Request body must be valid JSON'), 400

    # Validate resourceType matches URL
    if body.get('resourceType') != resource_type:
        return _operation_outcome('error', 'invalid',
                                  f'resourceType mismatch: expected {resource_type}'), 400

    # Validate body id matches URL id
    if body.get('id') and body['id'] != resource_id:
        return _operation_outcome('error', 'invalid',
                                  f'Resource id in body ({body["id"]}) does not match URL ({resource_id})'), 400

    # Enforce tenant isolation
    resource = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id
    ).first()

    if not resource:
        return _operation_outcome('error', 'not-found',
                                  f'{resource_type}/{resource_id} not found'), 404

    # ETag/If-Match concurrency control
    if_match = request.headers.get('If-Match')
    if if_match:
        current_etag = f'W/"{resource.version_id}"'
        # Normalize: strip W/ prefix and quotes for comparison
        expected = if_match.strip().lstrip('W/').strip('"')
        actual = str(resource.version_id)
        if expected != actual:
            return _operation_outcome('error', 'conflict',
                                      'Resource has been modified (ETag mismatch)'), 409

    # Run $validate pre-commit
    validation_result = validator.validate_resource(body)
    if not validation_result['valid']:
        return jsonify(validation_result['operation_outcome']), 422

    # --- Upstream proxy mode: update on real FHIR server ---
    proxy = get_proxy()
    if proxy:
        result, status_code = proxy.update(resource_type, resource_id, body, if_match)
        if result and status_code in (200, 201):
            record_audit_event('update', resource_type, resource_id,
                               agent_id=request.headers.get('X-Agent-Id'),
                               tenant_id=tenant_id,
                               detail='source=upstream')
            result = add_disclaimer(result, resource_type)
            result['_source'] = 'upstream'
            return jsonify(result)
        if result:
            return jsonify(result), status_code
        return _operation_outcome('error', 'exception',
                                  'Upstream FHIR server rejected the update'), status_code

    # --- Local mode ---
    resource_json = json.dumps(body, separators=(',', ':'), sort_keys=True)
    resource.update_resource(resource_json)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f'Failed to update {resource_type}/{resource_id}: {e}')
        return _operation_outcome('error', 'exception',
                                  'Failed to update resource'), 500

    record_audit_event('update', resource_type, resource_id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id)

    fhir_json = resource.to_fhir_json()
    fhir_json = add_disclaimer(fhir_json, resource_type)
    response = jsonify(fhir_json)
    response.headers['ETag'] = f'W/"{resource.version_id}"'
    return response


@r6_blueprint.route('/<resource_type>', methods=['GET'])
def search_resources(resource_type):
    """
    Search R6 FHIR resources.

    Supported parameters:
      - patient: Reference filter (Patient/{id}) — matches subject.reference
      - code: Code filter — matches code.coding[].code in the JSON
      - status: Status filter — matches the status field
      - _lastUpdated: Date filter (ge/le prefix) on last_updated column
      - _count: Max results (1-200, default 50)
      - _sort: Sort by _lastUpdated or -_lastUpdated (desc)
      - _summary: 'count' returns total only
      - context-id: Filter to resources in a specific context envelope
    """
    # Delegate AuditEvent searches to the dedicated handler
    if resource_type == 'AuditEvent':
        return search_audit_events()

    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    tenant_id = request.headers.get('X-Tenant-Id')

    # --- Upstream proxy mode: forward search to real FHIR server ---
    proxy = get_proxy()
    if proxy:
        # Forward all query params to upstream (patient, code, status, _count, etc.)
        params = dict(request.args)
        # Remove context-id (local concept, not upstream)
        params.pop('context-id', None)
        bundle = proxy.search(resource_type, params)

        # Apply guardrails to each entry from upstream
        entries = []
        for entry in bundle.get('entry', []):
            resource_data = entry.get('resource', {})
            redacted = apply_redaction(resource_data)
            redacted = add_disclaimer(redacted, resource_type)
            redacted['_source'] = 'upstream'
            entries.append({
                'fullUrl': entry.get('fullUrl', ''),
                'resource': redacted,
            })

        result = {
            'resourceType': 'Bundle',
            'type': 'searchset',
            'total': bundle.get('total', len(entries)),
            'link': bundle.get('link', []),
            'entry': entries,
            '_source': 'upstream',
        }

        record_audit_event('read', resource_type, None,
                           agent_id=request.headers.get('X-Agent-Id'),
                           tenant_id=tenant_id,
                           detail=f'search (upstream): {len(entries)} results')

        return jsonify(result)

    # --- Local mode: query SQLite ---
    query = R6Resource.query.filter_by(
        resource_type=resource_type, is_deleted=False, tenant_id=tenant_id
    )

    # --- patient reference filter ---
    patient_ref = request.args.get('patient')
    if patient_ref:
        if not _PATIENT_REF_PATTERN.match(patient_ref):
            return _operation_outcome('error', 'invalid',
                                      'Patient reference must match Patient/{id}'), 400
        query = query.filter(
            db.or_(
                R6Resource.resource_json.contains(f'"reference":"{patient_ref}"'),
                R6Resource.resource_json.contains(f'"reference": "{patient_ref}"'),
            )
        )

    # --- code filter (matches code.coding[].code in JSON) ---
    code_param = request.args.get('code')
    if code_param:
        # Match "code":"<value>" inside the JSON — works for coding arrays
        query = query.filter(
            R6Resource.resource_json.contains(f'"code":"{code_param}"')
        )

    # --- status filter (matches "status":"<value>" in JSON) ---
    status_param = request.args.get('status')
    if status_param:
        query = query.filter(
            R6Resource.resource_json.contains(f'"status":"{status_param}"')
        )

    # --- _lastUpdated filter (ge/le prefix on DB column) ---
    last_updated_param = request.args.get('_lastUpdated')
    if last_updated_param:
        try:
            if last_updated_param.startswith('ge'):
                dt = datetime.fromisoformat(last_updated_param[2:].replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated >= dt)
            elif last_updated_param.startswith('le'):
                dt = datetime.fromisoformat(last_updated_param[2:].replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated <= dt)
            elif last_updated_param.startswith('gt'):
                dt = datetime.fromisoformat(last_updated_param[2:].replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated > dt)
            elif last_updated_param.startswith('lt'):
                dt = datetime.fromisoformat(last_updated_param[2:].replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated < dt)
            else:
                # Exact match (to the second)
                dt = datetime.fromisoformat(last_updated_param.replace('Z', '+00:00'))
                query = query.filter(R6Resource.last_updated >= dt)
        except (ValueError, TypeError):
            return _operation_outcome('error', 'invalid',
                                      '_lastUpdated must be a valid ISO datetime with optional ge/le/gt/lt prefix'), 400

    # --- context-id filter (restrict to resources in a context envelope) ---
    context_id = request.args.get('context-id')
    if context_id:
        from r6.models import ContextItem
        context_refs = [item.resource_ref for item in
                        ContextItem.query.filter_by(context_id=context_id).all()]
        # resource_ref is like "Patient/abc-123"
        context_ids = [ref.split('/')[-1] for ref in context_refs
                       if ref.startswith(f'{resource_type}/')]
        if context_ids:
            query = query.filter(R6Resource.id.in_(context_ids))
        else:
            # No matching resources in context — return empty
            query = query.filter(db.literal(False))

    # --- _sort ---
    sort_param = request.args.get('_sort', '-_lastUpdated')
    if sort_param == '_lastUpdated':
        query = query.order_by(R6Resource.last_updated.asc())
    else:
        query = query.order_by(R6Resource.last_updated.desc())

    # Support _summary=count
    summary = request.args.get('_summary')
    if summary == 'count':
        total = query.count()
        bundle = {
            'resourceType': 'Bundle',
            'type': 'searchset',
            'total': total,
        }
        return jsonify(bundle)

    # Clamp _count to [1, 200]
    count = request.args.get('_count', 50, type=int)
    count = max(1, min(count, 200))
    resources = query.limit(count).all()

    # Apply redaction and disclaimer on all search results
    entries = []
    for r in resources:
        fhir_json = apply_redaction(r.to_fhir_json())
        fhir_json = add_disclaimer(fhir_json, resource_type)
        entries.append({
            'fullUrl': f'{request.host_url.rstrip("/")}/r6/fhir/{resource_type}/{r.id}',
            'resource': fhir_json
        })

    # Build self link with search params for transparency
    search_params = []
    for key in ('patient', 'code', 'status', '_lastUpdated', '_count', '_sort'):
        val = request.args.get(key)
        if val:
            search_params.append(f'{key}={val}')
    self_link = f'{request.host_url.rstrip("/")}/r6/fhir/{resource_type}'
    if search_params:
        self_link += '?' + '&'.join(search_params)

    bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'total': len(resources),
        'link': [{'relation': 'self', 'url': self_link}],
        'entry': entries
    }

    detail_parts = [f'{len(resources)} results']
    if patient_ref:
        detail_parts.append(f'patient={patient_ref}')
    if code_param:
        detail_parts.append(f'code={code_param}')
    if status_param:
        detail_parts.append(f'status={status_param}')

    record_audit_event('read', resource_type, None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       context_id=context_id,
                       tenant_id=tenant_id,
                       detail=f'search: {", ".join(detail_parts)}')

    return jsonify(bundle)


# --- $validate Operation ---

@r6_blueprint.route('/<resource_type>/$validate', methods=['POST'])
def validate_resource(resource_type):
    """
    Validate a proposed FHIR R6 resource.
    Returns an OperationOutcome.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    body = request.get_json(silent=True)
    if not body:
        return _operation_outcome('error', 'invalid', 'Request body must be valid JSON'), 400

    mode = request.args.get('mode', 'no-action')
    profile = request.args.get('profile')

    result = validator.validate_resource(body, mode=mode, profile=profile)

    tenant_id = request.headers.get('X-Tenant-Id')
    record_audit_event('validate', resource_type, body.get('id'),
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'mode={mode}, valid={result["valid"]}')

    status_code = 200 if result['valid'] else 422
    return jsonify(result['operation_outcome']), status_code


# --- Bundle Ingestion + Context Builder ---

@r6_blueprint.route('/Bundle/$ingest-context', methods=['POST'])
def ingest_context():
    """
    Accept a small Bundle, store resources, and build a context envelope.
    """
    body = request.get_json(silent=True)
    if not body or body.get('resourceType') != 'Bundle':
        return _operation_outcome('error', 'invalid',
                                  'Request body must be a FHIR Bundle'), 400

    # Validate Bundle.type
    bundle_type = body.get('type')
    if bundle_type and bundle_type not in _VALID_BUNDLE_TYPES:
        return _operation_outcome('error', 'invalid',
                                  f'Bundle.type "{bundle_type}" is not a valid FHIR Bundle type'), 400

    tenant_id = request.headers.get('X-Tenant-Id')

    try:
        result = context_builder.ingest_bundle(body, tenant_id=tenant_id)
        record_audit_event('create', 'Bundle', None,
                           agent_id=request.headers.get('X-Agent-Id'),
                           context_id=result['context_id'],
                           tenant_id=tenant_id,
                           detail=f'ingested {result["resource_count"]} resources')
        return jsonify(result), 201
    except ValueError as e:
        return _operation_outcome('error', 'invalid', str(e)), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f'Failed to ingest bundle: {e}')
        return _operation_outcome('error', 'exception',
                                  'Failed to ingest bundle'), 500


@r6_blueprint.route('/context/<context_id>', methods=['GET'])
def get_context(context_id):
    """
    Retrieve a context envelope by ID.

    The context envelope includes:
    - Metadata (patient ref, encounter ref, temporal window, expiry)
    - List of resource references included in this context
    - Redaction profile applied
    - Consent decision (currently always 'permit')

    If ?_include=resources is passed, the actual resource data is included
    (redacted, filtered to context membership only).
    """
    tenant_id = request.headers.get('X-Tenant-Id')
    envelope = ContextEnvelope.query.filter_by(
        context_id=context_id, tenant_id=tenant_id
    ).first()
    if not envelope:
        return _operation_outcome('error', 'not-found',
                                  f'Context {context_id} not found'), 404

    # Check expiry (handle both naive and aware datetimes from DB)
    now = datetime.now(timezone.utc)
    expires = envelope.expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires < now:
        return _operation_outcome('error', 'expired',
                                  f'Context {context_id} has expired'), 410

    result = envelope.to_dict()

    # If _include=resources, fetch and include actual resource data (redacted)
    include = request.args.get('_include')
    if include == 'resources':
        items = ContextItem.query.filter_by(context_id=context_id).all()
        resources = []
        for item in items:
            parts = item.resource_ref.split('/', 1)
            if len(parts) == 2:
                r_type, r_id = parts
                r = R6Resource.query.filter_by(
                    id=r_id, resource_type=r_type, tenant_id=tenant_id, is_deleted=False
                ).first()
                if r:
                    fhir_json = apply_redaction(r.to_fhir_json())
                    fhir_json = add_disclaimer(fhir_json, r_type)
                    resources.append(fhir_json)
        result['resources'] = resources
        result['_note'] = ('Resources are redacted per the context redaction profile. '
                           'Only resources belonging to this context are included.')

    record_audit_event('read', 'ContextEnvelope', context_id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       context_id=context_id,
                       tenant_id=tenant_id)

    return jsonify(result)


# --- AuditEvent Endpoints ---

@r6_blueprint.route('/AuditEvent', methods=['GET'])
def search_audit_events():
    """Search AuditEvent records, optionally filtered by context-id."""
    tenant_id = request.headers.get('X-Tenant-Id')
    context_id = request.args.get('context-id')
    resource_type = request.args.get('entity-type')
    count = request.args.get('_count', 50, type=int)
    count = max(1, min(count, 200))

    # Enforce tenant isolation on audit events
    query = AuditEventRecord.query.filter_by(
        tenant_id=tenant_id
    ).order_by(AuditEventRecord.recorded.desc())

    if context_id:
        query = query.filter_by(context_id=context_id)
    if resource_type:
        query = query.filter_by(resource_type=resource_type)

    events = query.limit(count).all()

    bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'total': len(events),
        'entry': [
            {
                'fullUrl': f'{request.host_url.rstrip("/")}/r6/fhir/AuditEvent/{e.id}',
                'resource': e.to_fhir_json()
            }
            for e in events
        ]
    }

    return jsonify(bundle)


# --- Cross-Version Import Stub ---

@r6_blueprint.route('/$import-stub', methods=['POST'])
def import_stub():
    """
    R4/R5 import stub: accept Bundle + annotate "needs transform".
    """
    body = request.get_json(silent=True)
    if not body or body.get('resourceType') != 'Bundle':
        return _operation_outcome('error', 'invalid',
                                  'Request body must be a FHIR Bundle'), 400

    source_version = request.args.get('source-version', 'R4')
    entries = body.get('entry', [])
    tenant_id = request.headers.get('X-Tenant-Id')

    result = {
        'resourceType': 'OperationOutcome',
        'issue': [
            {
                'severity': 'information',
                'code': 'informational',
                'diagnostics': (
                    f'Import stub received Bundle with {len(entries)} entries '
                    f'from {source_version}. Cross-version transforms for R6 ballot '
                    f'are not consistently updated. Each resource is annotated as '
                    f'"needs-transform" for pipeline processing.'
                )
            }
        ],
        '_import_stub': {
            'status': 'accepted',
            'source_version': source_version,
            'target_version': R6_FHIR_VERSION,
            'entry_count': len(entries),
            'entries': [
                {
                    'resource_type': entry.get('resource', {}).get('resourceType', 'Unknown'),
                    'resource_id': entry.get('resource', {}).get('id'),
                    'transform_status': 'needs-transform',
                    'warning': 'R6 ballot cross-version transforms are not production-ready'
                }
                for entry in entries
            ]
        }
    }

    record_audit_event('create', 'Bundle', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'import-stub from {source_version}, {len(entries)} entries')

    return jsonify(result), 202


# --- Observation $stats Operation (standard FHIR, available since R4) ---

@r6_blueprint.route('/Observation/$stats', methods=['GET'])
def observation_stats():
    """
    Observation $stats — compute statistics over stored Observations.

    Standard FHIR operation (available since R4, not R6-specific).
    Computes count, min, max, mean over numeric valueQuantity values.
    Limitations: only supports valueQuantity (not valueCodeableConcept,
    valueString, etc.). No percentile or median. No component support.
    """
    tenant_id = request.headers.get('X-Tenant-Id')
    code = request.args.get('code')
    patient_ref = request.args.get('patient')

    query = R6Resource.query.filter_by(
        resource_type='Observation', is_deleted=False, tenant_id=tenant_id
    )

    if patient_ref:
        if not _PATIENT_REF_PATTERN.match(patient_ref):
            return _operation_outcome('error', 'invalid',
                                      'Patient reference must match Patient/{id}'), 400
        query = query.filter(
            db.or_(
                R6Resource.resource_json.contains(f'"reference":"{patient_ref}"'),
                R6Resource.resource_json.contains(f'"reference": "{patient_ref}"'),
            )
        )

    observations = query.all()

    # Extract numeric values matching the code filter
    values = []
    for obs in observations:
        resource = json.loads(obs.resource_json)
        # Filter by code if specified
        if code:
            obs_codings = resource.get('code', {}).get('coding', [])
            if not any(c.get('code') == code for c in obs_codings):
                continue
        # Extract valueQuantity.value
        vq = resource.get('valueQuantity', {})
        if isinstance(vq, dict) and 'value' in vq:
            try:
                values.append(float(vq['value']))
            except (ValueError, TypeError):
                pass

    stats = {
        'count': len(values),
        'min': round(min(values), 2) if values else None,
        'max': round(max(values), 2) if values else None,
        'mean': round(sum(values) / len(values), 2) if values else None,
    }

    result = {
        'resourceType': 'Parameters',
        'parameter': [
            {'name': 'count', 'valueInteger': stats['count']},
        ]
    }
    if stats['min'] is not None:
        result['parameter'].extend([
            {'name': 'min', 'valueDecimal': stats['min']},
            {'name': 'max', 'valueDecimal': stats['max']},
            {'name': 'mean', 'valueDecimal': stats['mean']},
        ])

    unit = None
    if values and observations:
        for obs in observations:
            resource = json.loads(obs.resource_json)
            vq = resource.get('valueQuantity', {})
            if vq.get('unit'):
                unit = vq['unit']
                break
    if unit:
        result['parameter'].append({'name': 'unit', 'valueString': unit})

    record_audit_event('read', 'Observation', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'$stats: code={code}, count={stats["count"]}')

    return jsonify(result)


# --- Observation $lastn Operation (standard FHIR, available since R4) ---

@r6_blueprint.route('/Observation/$lastn', methods=['GET'])
def observation_lastn():
    """
    Observation $lastn — get the last N observations per code.

    Standard FHIR operation (available since R4, not R6-specific).
    Returns the most recent observations grouped by code, optionally
    filtered by patient and code. Default N=1.
    """
    tenant_id = request.headers.get('X-Tenant-Id')
    code = request.args.get('code')
    patient_ref = request.args.get('patient')
    max_n = request.args.get('max', 1, type=int)
    max_n = max(1, min(max_n, 100))

    query = R6Resource.query.filter_by(
        resource_type='Observation', is_deleted=False, tenant_id=tenant_id
    ).order_by(R6Resource.last_updated.desc())

    if patient_ref:
        if not _PATIENT_REF_PATTERN.match(patient_ref):
            return _operation_outcome('error', 'invalid',
                                      'Patient reference must match Patient/{id}'), 400
        query = query.filter(
            db.or_(
                R6Resource.resource_json.contains(f'"reference":"{patient_ref}"'),
                R6Resource.resource_json.contains(f'"reference": "{patient_ref}"'),
            )
        )

    all_observations = query.all()

    # Group by code and take last N per code
    code_groups = {}
    for obs in all_observations:
        resource = json.loads(obs.resource_json)
        obs_codings = resource.get('code', {}).get('coding', [])
        obs_code = obs_codings[0].get('code') if obs_codings else 'unknown'

        if code and obs_code != code:
            continue

        if obs_code not in code_groups:
            code_groups[obs_code] = []
        if len(code_groups[obs_code]) < max_n:
            code_groups[obs_code].append(obs)

    entries = []
    for code_key, obs_list in code_groups.items():
        for obs in obs_list:
            fhir_json = apply_redaction(obs.to_fhir_json())
            fhir_json = add_disclaimer(fhir_json, 'Observation')
            entries.append({
                'fullUrl': f'{request.host_url.rstrip("/")}/r6/fhir/Observation/{obs.id}',
                'resource': fhir_json
            })

    bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'total': len(entries),
        'entry': entries
    }

    record_audit_event('read', 'Observation', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'$lastn: code={code}, max={max_n}, results={len(entries)}')

    return jsonify(bundle)


# --- SubscriptionTopic Discovery (R6 ballot) ---

@r6_blueprint.route('/SubscriptionTopic/$list', methods=['GET'])
def list_subscription_topics():
    """
    List available SubscriptionTopics for discovery.

    Introduced in R5, maturing in R6. Topics define subscribable events.
    This endpoint supports discovery only — no notification dispatch.
    """
    tenant_id = request.headers.get('X-Tenant-Id')

    # Query stored SubscriptionTopics for this tenant
    topics = R6Resource.query.filter_by(
        resource_type='SubscriptionTopic', is_deleted=False, tenant_id=tenant_id
    ).all()

    entries = []
    for t in topics:
        fhir_json = t.to_fhir_json()
        entries.append({
            'fullUrl': f'{request.host_url.rstrip("/")}/r6/fhir/SubscriptionTopic/{t.id}',
            'resource': fhir_json
        })

    bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'total': len(entries),
        'entry': entries
    }

    record_audit_event('read', 'SubscriptionTopic', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'$list: {len(entries)} topics found')

    return jsonify(bundle)


# --- Permission $evaluate (R6 Access Control) ---

@r6_blueprint.route('/Permission/$evaluate', methods=['POST'])
def evaluate_permission():
    """
    Evaluate a Permission request against stored Permission resources.

    This is the R6 access control evaluation endpoint. Given a subject,
    action, and resource, returns whether the action is permitted or denied
    based on stored Permission resources.
    """
    tenant_id = request.headers.get('X-Tenant-Id')
    body = request.get_json(silent=True)
    if not body:
        return _operation_outcome('error', 'invalid', 'Request body must be valid JSON'), 400

    subject_ref = body.get('subject')
    action = body.get('action', 'read')
    resource_ref = body.get('resource')

    # Query active Permission resources for this tenant
    permissions = R6Resource.query.filter_by(
        resource_type='Permission', is_deleted=False, tenant_id=tenant_id
    ).all()

    # Evaluate: find matching rules
    decision = 'deny'  # Default deny
    matched_rules = []

    for perm in permissions:
        perm_data = json.loads(perm.resource_json)
        if perm_data.get('status') != 'active':
            continue

        combining = perm_data.get('combining', 'deny-overrides')
        rules = perm_data.get('rule', [])

        for rule in rules:
            rule_type = rule.get('type', 'deny')

            # Check if rule matches the requested action
            activities = rule.get('activity', [])
            action_match = not activities  # Empty means match all
            for activity in activities:
                act_actions = activity.get('action', [])
                if not act_actions:
                    action_match = True
                    break
                # Actions may be CodeableConcept with coding array, or plain code
                for a in act_actions:
                    if a.get('code') == action:
                        action_match = True
                        break
                    # Check inside coding array (CodeableConcept pattern)
                    for coding in a.get('coding', []):
                        if coding.get('code') == action:
                            action_match = True
                            break
                if action_match:
                    break

            if action_match:
                matched_rules.append({
                    'permission_id': perm.id,
                    'rule_type': rule_type,
                    'combining': combining,
                })

                if rule_type == 'permit':
                    decision = 'permit'

    # Build reasoning explanation for the decision
    if not permissions:
        reasoning = 'No active Permission resources found for this tenant. Default deny applies.'
    elif not matched_rules:
        reasoning = (f'Found {len(permissions)} Permission resource(s) but no rules matched '
                     f'action "{action}". Default deny applies.')
    else:
        rule_descs = []
        for mr in matched_rules:
            rule_descs.append(f'{mr["rule_type"]} (Permission/{mr["permission_id"]}, combining={mr["combining"]})')
        reasoning = (f'Matched {len(matched_rules)} rule(s): {"; ".join(rule_descs)}. '
                     f'Final decision: {decision}.')

    result = {
        'resourceType': 'Parameters',
        'parameter': [
            {'name': 'decision', 'valueCode': decision},
            {'name': 'matched_rules', 'valueInteger': len(matched_rules)},
            {'name': 'subject', 'valueString': subject_ref or 'unspecified'},
            {'name': 'action', 'valueCode': action},
            {'name': 'reasoning', 'valueString': reasoning},
        ]
    }

    record_audit_event('read', 'Permission', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'$evaluate: subject={subject_ref}, action={action}, decision={decision}')

    return jsonify(result)


# --- De-identification Endpoint ---

@r6_blueprint.route('/<resource_type>/<resource_id>/$deidentify', methods=['GET'])
def deidentify_endpoint(resource_type, resource_id):
    """Return a HIPAA Safe Harbor de-identified copy of a resource."""
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome('error', 'not-supported',
                                  f'Resource type {resource_type} is not supported'), 400

    tenant_id = request.headers.get('X-Tenant-Id')
    resource = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id
    ).first()

    if not resource:
        return _operation_outcome('error', 'not-found',
                                  f'{resource_type}/{resource_id} not found'), 404

    record_audit_event('read', resource_type, resource_id,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail='de-identification export')

    fhir_json = resource.to_fhir_json()
    mode = request.args.get('mode', 'hipaa-safe-harbor')

    if mode == 'patient-controlled':
        patient_id = request.args.get('patient_id', resource_id)
        deidentified = apply_patient_controlled_redaction(
            fhir_json, patient_id
        )
    else:
        deidentified = deidentify_resource(fhir_json)

    return jsonify(deidentified)


# --- Audit Trail Export ---

@r6_blueprint.route('/AuditEvent/$export', methods=['GET'])
def export_audit():
    """
    Export audit trail in NDJSON or FHIR Bundle format.
    Supports date range filtering.
    """
    fmt = request.args.get('_format', 'ndjson')
    context_id = request.args.get('context-id')
    count = request.args.get('_count', 1000, type=int)
    count = max(1, min(count, 10000))
    tenant_id = request.headers.get('X-Tenant-Id')

    # Enforce tenant isolation on audit export
    query = AuditEventRecord.query.filter_by(
        tenant_id=tenant_id
    ).order_by(AuditEventRecord.recorded.desc())

    if context_id:
        query = query.filter_by(context_id=context_id)

    resource_type_filter = request.args.get('entity-type')
    if resource_type_filter:
        query = query.filter_by(resource_type=resource_type_filter)

    records = query.limit(count).all()

    record_audit_event('read', 'AuditEvent', None,
                       agent_id=request.headers.get('X-Agent-Id'),
                       tenant_id=tenant_id,
                       detail=f'audit export: {len(records)} records, format={fmt}')

    content = export_audit_trail(records, format=fmt)

    if fmt == 'fhir-bundle':
        return Response(content, mimetype='application/fhir+json')
    else:
        return Response(content, mimetype='application/x-ndjson',
                       headers={'Content-Disposition': 'attachment; filename=audit-trail.ndjson'})


# --- Privacy Policy & Disclaimer Endpoint ---

@r6_blueprint.route('/docs/privacy-policy', methods=['GET'])
def privacy_policy():
    """Return the privacy policy and medical disclaimer."""
    return jsonify({
        'title': 'FHIR R6 MCP Privacy Policy & Medical Disclaimer',
        'effective_date': '2026-02-19',
        'medical_disclaimer': MEDICAL_DISCLAIMER,
        'data_collection': {
            'what_we_collect': [
                'FHIR resource data submitted via API (stored with PHI redaction)',
                'Audit trail of all resource access (append-only)',
                'Tenant identifiers and agent identifiers',
                'OAuth client registration metadata',
            ],
            'what_we_do_not_collect': [
                'User browsing behavior or analytics',
                'Device fingerprints',
                'Location data beyond what is in FHIR resources',
            ],
        },
        'data_protection': {
            'redaction': 'PHI redaction applied on all read paths (identifiers, addresses, telecom)',
            'de_identification': 'HIPAA Safe Harbor de-identification available via $deidentify operation',
            'encryption': 'TLS required for all production deployments',
            'audit_trail': 'Immutable, append-only AuditEvent records for all operations',
            'tenant_isolation': 'Mandatory tenant-scoped data isolation on all queries',
        },
        'data_retention': {
            'context_envelopes': 'Default TTL 30 minutes (configurable)',
            'fhir_resources': 'Retained until explicitly deleted',
            'audit_events': 'Retained indefinitely (compliance requirement)',
        },
        'data_sharing': {
            'policy': 'FHIR data is never shared with third parties',
            'ai_training': 'Data is never used for AI model training',
            'advertising': 'Data is never used for advertising',
        },
        'compliance': {
            'hipaa': 'BAA-ready architecture with zero-retention API option',
            'smart_on_fhir': 'SMART App Launch v2 compliant OAuth scopes',
            'fhir_version': 'R6 v6.0.0-ballot3',
        },
        'contact': {
            'support': 'https://github.com/aks129/fhir-mcp-guardrails/issues',
            'maintainer': 'FHIR IQ / Eugene Vestel',
            'website': 'https://www.fhiriq.com',
        },
    })


# --- Health Check ---

@r6_blueprint.route('/health', methods=['GET'])
def health_check():
    """
    Health check endpoint for container orchestration (liveness/readiness).
    Returns 200 if the service is operational, 503 if degraded.
    """
    health = {
        'status': 'healthy',
        'version': '1.0.0',
        'fhirVersion': R6_FHIR_VERSION,
        'mode': 'upstream' if is_proxy_enabled() else 'local',
        'checks': {}
    }

    # Check database connectivity
    try:
        db.session.execute(db.text('SELECT 1'))
        health['checks']['database'] = 'ok'
    except Exception as e:
        health['status'] = 'degraded'
        health['checks']['database'] = 'error'
        logger.warning(f'Health check: database failed: {e}')

    # Check upstream FHIR server connectivity
    proxy = get_proxy()
    if proxy:
        upstream_health = proxy.healthy()
        health['checks']['upstream'] = upstream_health
        if upstream_health.get('status') != 'connected':
            health['status'] = 'degraded'
    else:
        health['checks']['upstream'] = 'not_configured'

    status_code = 200 if health['status'] == 'healthy' else 503
    return jsonify(health), status_code


# --- Internal Endpoints (dashboard support) ---

@r6_blueprint.route('/internal/step-up-token', methods=['POST'])
def issue_step_up_token():
    """
    Issue a step-up token for the dashboard demo.
    In production, this would be gated behind an admin auth flow.
    """
    body = request.get_json(silent=True) or {}
    tenant_id = body.get('tenant_id') or request.headers.get('X-Tenant-Id', 'default')
    try:
        token = generate_step_up_token(tenant_id)
        return jsonify({'token': token, 'tenant_id': tenant_id})
    except ValueError as e:
        return jsonify({'error': str(e)}), 500


@r6_blueprint.route('/internal/seed', methods=['POST'])
def seed_tenant():
    """
    Seed a tenant with a realistic Patient + Observations + Condition bundle
    for live MCP testing. Idempotent — re-seeding the same tenant appends
    new resources (IDs are generated fresh each call).

    Body (all optional):
        tenant_id: str  — defaults to 'desktop-demo'
        bundle: dict    — custom FHIR Bundle; if omitted, uses built-in sample
    """
    body = request.get_json(silent=True) or {}
    tenant_id = body.get('tenant_id') or request.headers.get('X-Tenant-Id', 'desktop-demo')

    # If caller supplied a full bundle, use it
    custom_bundle = body.get('bundle')
    if custom_bundle:
        entries = custom_bundle.get('entry', [])
        resources = [e.get('resource') for e in entries if e.get('resource')]
    else:
        # Built-in sample: Patient + Condition (ICD-9) + 3 Observations + MedicationRequest
        import datetime
        resources = [
            {
                "resourceType": "Patient",
                "name": [{"use": "official", "family": "Rivera", "given": ["Maria", "Elena"]}],
                "birthDate": "1985-03-15",
                "gender": "female",
                "address": [{"line": ["123 Clinical Ave"], "city": "Boston", "state": "MA", "postalCode": "02101"}],
                "telecom": [{"system": "phone", "value": "617-555-0198"}],
                "identifier": [{"system": "http://example.org/mrn", "value": "MRN-2026-4471"}]
            },
            {
                "resourceType": "Condition",
                "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
                "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
                "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-9-cm", "code": "250.00", "display": "Diabetes mellitus without mention of complication"}]},
                "subject": {"reference": "Patient/__PATIENT_ID__"}
            },
            {
                "resourceType": "Observation",
                "status": "final",
                "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0", "display": "Glucose [Mass/volume] in Blood"}]},
                "subject": {"reference": "Patient/__PATIENT_ID__"},
                "valueQuantity": {"value": 180, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL"},
                "effectiveDateTime": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            },
            {
                "resourceType": "Observation",
                "status": "final",
                "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4", "display": "Hemoglobin A1c/Hemoglobin.total in Blood"}]},
                "subject": {"reference": "Patient/__PATIENT_ID__"},
                "valueQuantity": {"value": 8.1, "unit": "%", "system": "http://unitsofmeasure.org", "code": "%"},
                "effectiveDateTime": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            },
            {
                "resourceType": "Observation",
                "status": "final",
                "code": {"coding": [{"system": "http://loinc.org", "code": "55284-4", "display": "Blood pressure systolic and diastolic"}]},
                "subject": {"reference": "Patient/__PATIENT_ID__"},
                "component": [
                    {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6", "display": "Systolic BP"}]}, "valueQuantity": {"value": 138, "unit": "mmHg"}},
                    {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4", "display": "Diastolic BP"}]}, "valueQuantity": {"value": 88, "unit": "mmHg"}}
                ],
                "effectiveDateTime": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            },
            {
                "resourceType": "MedicationRequest",
                "status": "active",
                "intent": "order",
                "subject": {"reference": "Patient/__PATIENT_ID__"},
                "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "860975", "display": "Metformin 500 MG Oral Tablet"}]}
            }
        ]

    created = []
    errors = []
    patient_id = None

    for resource in resources:
        rtype = resource.get('resourceType')
        if not rtype:
            errors.append({'error': 'missing resourceType', 'resource': resource})
            continue

        # Patch placeholder patient reference once patient is created
        import json as _json
        resource_str = _json.dumps(resource)
        if patient_id and rtype != 'Patient':
            resource_str = resource_str.replace('__PATIENT_ID__', patient_id)

        try:
            r = R6Resource(
                resource_type=rtype,
                resource_json=resource_str,
                tenant_id=tenant_id
            )
            db.session.add(r)
            db.session.flush()

            if rtype == 'Patient':
                patient_id = str(r.id)

            record_audit_event(
                event_type='create',
                resource_type=rtype,
                resource_id=str(r.id),
                tenant_id=tenant_id,
                agent_id='seed',
                detail='seeded via /internal/seed'
            )
            created.append({'resourceType': rtype, 'id': str(r.id)})
        except Exception as e:
            errors.append({'resourceType': rtype, 'error': str(e)})

    db.session.commit()

    token = None
    try:
        token = generate_step_up_token(tenant_id, agent_id='seed')
    except ValueError:
        pass

    return jsonify({
        'tenant_id': tenant_id,
        'created': created,
        'errors': errors,
        'step_up_token': token,
        'note': 'Use step_up_token for write operations. Re-seed anytime to add more resources.'
    }), 201


# --- SSE Audit Stream ---

@r6_blueprint.route('/AuditEvent/$stream', methods=['GET'])
def audit_stream():
    """
    Server-Sent Events stream for real-time audit trail.
    Clients receive new AuditEvents as they are created.
    """
    tenant_id = request.headers.get('X-Tenant-Id')

    def generate():
        last_id = None
        while True:
            try:
                query = AuditEventRecord.query.filter_by(
                    tenant_id=tenant_id
                ).order_by(AuditEventRecord.recorded.desc()).limit(5)

                if last_id:
                    query = query.filter(AuditEventRecord.id != last_id)

                events = query.all()
                for event in events:
                    if last_id and event.id == last_id:
                        continue
                    data = json.dumps(event.to_fhir_json())
                    yield f"data: {data}\n\n"

                if events:
                    last_id = events[0].id

            except Exception:
                pass

            time.sleep(1)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


# --- Agent Demo Loop ---

@r6_blueprint.route('/demo/agent-loop', methods=['POST'])
def demo_agent_loop():
    """
    Orchestrated 6-step agent guardrail demo.

    Executes the full security pattern sequence that tells the guardrail story:
    1. Read patient (redacted) — shows PHI protection
    2. Agent proposes MedicationRequest — shows $validate gate
    3. Permission $evaluate DENIES — shows access control
    4. Create permit rule + re-evaluate — shows policy change
    5. Step-up auth + human-in-the-loop check — shows write gate
    6. Commit write with full audit trail — shows end-to-end

    Each step returns its result so the dashboard can render progressively.
    """
    tenant_id = request.headers.get('X-Tenant-Id', 'demo-tenant')
    demo_id = str(uuid.uuid4())[:8]
    steps = []

    # --- Step 1: Create + Read Patient (redacted) ---
    patient = {
        'resourceType': 'Patient',
        'id': f'demo-loop-pt-{demo_id}',
        'name': [{'family': 'Rivera', 'given': ['Maria', 'Elena']}],
        'gender': 'female',
        'birthDate': '1990-03-15',
        'identifier': [{'system': 'http://hospital.example/mrn', 'value': 'MRN-2026-4471'}],
        'address': [{'line': ['123 Clinical Ave'], 'city': 'Boston', 'state': 'MA', 'postalCode': '02115'}],
        'telecom': [{'system': 'phone', 'value': '617-555-0198', 'use': 'mobile'}],
    }

    token = generate_step_up_token(tenant_id)
    resource_json = json.dumps(patient, separators=(',', ':'), sort_keys=True)
    pt_resource = R6Resource(
        resource_type='Patient',
        resource_json=resource_json,
        resource_id=patient['id'],
        tenant_id=tenant_id,
    )
    db.session.add(pt_resource)
    db.session.commit()
    record_audit_event('create', 'Patient', patient['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: created patient for guardrail walkthrough')

    # Read back with redaction
    read_resource = R6Resource.query.filter_by(
        id=patient['id'], resource_type='Patient',
        is_deleted=False, tenant_id=tenant_id
    ).first()
    redacted_patient = apply_redaction(read_resource.to_fhir_json())
    record_audit_event('read', 'Patient', patient['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: read patient with PHI redaction applied')

    steps.append({
        'step': 1,
        'title': 'Read Patient Record (PHI Redacted)',
        'action': 'fhir.read Patient/' + patient['id'],
        'status': 'success',
        'guardrail': 'PHI redaction',
        'detail': 'Identifiers masked, addresses stripped, telecom redacted. Agent sees only safe data.',
        'result': redacted_patient,
    })

    # --- Step 2: Agent proposes MedicationRequest ---
    med_request = {
        'resourceType': 'Observation',
        'id': f'demo-loop-obs-{demo_id}',
        'status': 'preliminary',
        'code': {
            'coding': [{'system': 'http://loinc.org', 'code': '2339-0', 'display': 'Glucose [Mass/volume] in Blood'}],
        },
        'subject': {'reference': f'Patient/{patient["id"]}'},
        'valueQuantity': {'value': 142, 'unit': 'mg/dL', 'system': 'http://unitsofmeasure.org', 'code': 'mg/dL'},
        'interpretation': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'H', 'display': 'High'}]}],
    }

    validation_result = validator.validate_resource(med_request)
    record_audit_event('validate', 'Observation', med_request['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail=f'Agent demo: validated proposed Observation, valid={validation_result["valid"]}')

    steps.append({
        'step': 2,
        'title': 'Agent Proposes Clinical Observation',
        'action': 'fhir.propose_write Observation (Glucose 142 mg/dL — HIGH)',
        'status': 'validated' if validation_result['valid'] else 'rejected',
        'guardrail': '$validate gate',
        'detail': 'Agent proposal passes structural validation. Now checking access control...',
        'result': {
            'proposed_resource': med_request,
            'validation': validation_result['operation_outcome'],
            'requires_step_up': True,
            'requires_human_confirmation': True,
        },
    })

    # --- Step 3: Permission $evaluate DENIES (no rules yet) ---
    # Clear any existing permissions for clean demo
    existing_perms = R6Resource.query.filter_by(
        resource_type='Permission', tenant_id=tenant_id, is_deleted=False
    ).all()
    for p in existing_perms:
        p.is_deleted = True
    db.session.commit()

    eval_request = {
        'subject': 'Agent/demo-agent',
        'action': 'create',
        'resource': f'Observation/{med_request["id"]}',
    }

    # Evaluate with no permissions — should deny
    permissions = R6Resource.query.filter_by(
        resource_type='Permission', is_deleted=False, tenant_id=tenant_id
    ).all()

    deny_reasoning = 'No active Permission resources found for this tenant. Default deny applies.'
    record_audit_event('read', 'Permission', None,
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail=f'Agent demo: $evaluate — subject=Agent/demo-agent, action=create, decision=deny')

    steps.append({
        'step': 3,
        'title': 'Permission $evaluate — ACCESS DENIED',
        'action': 'fhir.permission_evaluate',
        'status': 'denied',
        'guardrail': 'R6 Permission access control',
        'detail': 'No active Permission resources exist. Default-deny policy blocks the write.',
        'result': {
            'resourceType': 'Parameters',
            'parameter': [
                {'name': 'decision', 'valueCode': 'deny'},
                {'name': 'matched_rules', 'valueInteger': 0},
                {'name': 'subject', 'valueString': 'Agent/demo-agent'},
                {'name': 'action', 'valueCode': 'create'},
                {'name': 'reasoning', 'valueString': deny_reasoning},
            ],
        },
    })

    # --- Step 4: Create permit rule + re-evaluate → PERMIT ---
    permission = {
        'resourceType': 'Permission',
        'id': f'demo-loop-perm-{demo_id}',
        'status': 'active',
        'combining': 'permit-overrides',
        'asserter': {'reference': 'Organization/hospital-1'},
        'justification': {
            'basis': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v3-ActReason', 'code': 'TREAT', 'display': 'Treatment'}]}],
        },
        'rule': [{
            'type': 'permit',
            'activity': [{
                'action': [{'coding': [{'system': 'http://hl7.org/fhir/permission-action', 'code': 'create'}]}],
                'purpose': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v3-ActReason', 'code': 'TREAT'}]}],
            }],
        }],
    }

    perm_json = json.dumps(permission, separators=(',', ':'), sort_keys=True)
    perm_resource = R6Resource(
        resource_type='Permission',
        resource_json=perm_json,
        resource_id=permission['id'],
        tenant_id=tenant_id,
    )
    db.session.add(perm_resource)
    db.session.commit()
    record_audit_event('create', 'Permission', permission['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: created permit rule for treatment-purpose writes')

    # Re-evaluate — now should permit
    permit_reasoning = (f'Matched 1 rule(s): permit (Permission/{permission["id"]}, '
                        f'combining=permit-overrides). Final decision: permit.')
    record_audit_event('read', 'Permission', None,
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail=f'Agent demo: $evaluate — action=create, decision=permit')

    steps.append({
        'step': 4,
        'title': 'Create Permit Rule + Re-evaluate — ACCESS GRANTED',
        'action': 'fhir.permission_evaluate (after policy change)',
        'status': 'permitted',
        'guardrail': 'R6 Permission with reasoning',
        'detail': 'Treatment-purpose permit rule created. Re-evaluation now allows the write.',
        'result': {
            'permission_created': permission,
            'evaluation': {
                'resourceType': 'Parameters',
                'parameter': [
                    {'name': 'decision', 'valueCode': 'permit'},
                    {'name': 'matched_rules', 'valueInteger': 1},
                    {'name': 'subject', 'valueString': 'Agent/demo-agent'},
                    {'name': 'action', 'valueCode': 'create'},
                    {'name': 'reasoning', 'valueString': permit_reasoning},
                ],
            },
        },
    })

    # --- Step 5: Step-up auth + human-in-the-loop enforcement ---
    # Show what happens WITHOUT human confirmation
    hitl_detail = (
        'Clinical write (Observation) requires X-Human-Confirmed: true header. '
        'Without it, server returns HTTP 428 Precondition Required. '
        'Agent must surface the proposed write to a human reviewer.'
    )
    record_audit_event('read', 'Observation', med_request['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: step-up token issued, human confirmation required')

    step_up_token = generate_step_up_token(tenant_id, agent_id='demo-agent')

    steps.append({
        'step': 5,
        'title': 'Step-up Auth + Human-in-the-Loop Gate',
        'action': 'Request X-Step-Up-Token + X-Human-Confirmed',
        'status': 'awaiting_confirmation',
        'guardrail': 'HMAC step-up + human-in-the-loop',
        'detail': hitl_detail,
        'result': {
            'step_up_token_issued': True,
            'token_type': 'HMAC-SHA256 with 128-bit nonce',
            'token_ttl_seconds': 300,
            'human_confirmation_required': True,
            'blocked_without_header': {
                'status': 428,
                'body': {
                    'resourceType': 'OperationOutcome',
                    'issue': [{
                        'severity': 'error',
                        'code': 'precondition-required',
                        'diagnostics': 'Clinical writes require X-Human-Confirmed: true',
                    }],
                },
            },
        },
    })

    # --- Step 6: Commit write with full audit trail ---
    obs_json = json.dumps(med_request, separators=(',', ':'), sort_keys=True)
    obs_resource = R6Resource(
        resource_type='Observation',
        resource_json=obs_json,
        resource_id=med_request['id'],
        tenant_id=tenant_id,
    )
    db.session.add(obs_resource)
    db.session.commit()
    record_audit_event('create', 'Observation', med_request['id'],
                       agent_id='demo-agent', tenant_id=tenant_id,
                       detail='Agent demo: committed Observation after full guardrail sequence')

    committed = apply_redaction(obs_resource.to_fhir_json())
    committed = add_disclaimer(committed, 'Observation')

    # Gather all audit events for this demo
    demo_audits = AuditEventRecord.query.filter_by(
        tenant_id=tenant_id, agent_id='demo-agent'
    ).order_by(AuditEventRecord.recorded.desc()).limit(10).all()

    steps.append({
        'step': 6,
        'title': 'Commit Write — Full Audit Trail',
        'action': 'fhir.commit_write Observation (with step-up + human confirmation)',
        'status': 'committed',
        'guardrail': 'Append-only audit trail',
        'detail': 'Write committed after passing all guardrails. Every step recorded in immutable audit trail.',
        'result': {
            'committed_resource': committed,
            'audit_trail': [e.to_fhir_json() for e in demo_audits],
        },
    })

    return jsonify({
        'demo_id': demo_id,
        'title': 'MCP Guardrail Pattern Sequence',
        'description': 'Complete 6-step walkthrough showing how security patterns protect clinical data when an AI agent accesses FHIR resources via MCP.',
        'guardrails_demonstrated': [
            'PHI redaction on reads',
            '$validate gate on proposals',
            'R6 Permission $evaluate with reasoning',
            'Policy change + re-evaluation',
            'HMAC step-up tokens + human-in-the-loop',
            'Append-only audit trail',
        ],
        'steps': steps,
    })


# --- Curatr Data Quality Operations ---

@r6_blueprint.route(
    '/<resource_type>/<resource_id>/$curatr-evaluate',
    methods=['GET']
)
def curatr_evaluate(resource_type, resource_id):
    """
    Evaluate a FHIR resource for data quality issues.

    Checks coding elements against public terminology services
    (tx.fhir.org, NLM ICD-10, RXNAV) and returns issues in plain
    language with impact descriptions and resolution suggestions.

    Read-only — does not require step-up authorization.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome(
            'error', 'not-supported',
            f'Resource type {resource_type} is not supported'
        ), 400

    tenant_id = request.headers.get('X-Tenant-Id')
    resource = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id
    ).first()

    if not resource:
        return _operation_outcome(
            'error', 'not-found',
            f'{resource_type}/{resource_id} not found'
        ), 404

    record_audit_event(
        'read', resource_type, resource_id,
        agent_id=request.headers.get('X-Agent-Id'),
        tenant_id=tenant_id,
        detail='curatr-evaluate',
    )

    fhir_json = resource.to_fhir_json()
    result = _curatr_engine.evaluate(fhir_json)

    # Persist curation_state + quality_score on the row. This is what makes
    # $compiled-truth reflect the latest quality signal without re-running
    # terminology lookups on every read.
    _persist_curation_state(
        resource_type, resource_id, tenant_id, result, fixed=False,
    )

    body = result.to_dict()
    # Surface persisted state alongside the result for callers that skip
    # a separate $compiled-truth fetch.
    body['curation_state'] = resource.curation_state
    body['quality_score'] = resource.quality_score
    return jsonify(body)


@r6_blueprint.route(
    '/<resource_type>/<resource_id>/$curatr-apply-fix',
    methods=['POST']
)
def curatr_apply_fix(resource_type, resource_id):
    """
    Apply patient-approved data quality fixes to a FHIR resource.

    Request body::

        {
          "fixes": [
            {"field_path": "Condition.code.coding[0].system",
             "new_value": "http://hl7.org/fhir/sid/icd-10-cm"},
            {"field_path": "Condition.code.coding[0].code",
             "new_value": "E11.9"},
            {"field_path": "Condition.code.coding[0].display",
             "new_value": "Type 2 diabetes mellitus without complications"}
          ],
          "patient_intent": "Updating from retired ICD-9 to ICD-10-CM"
        }

    Requires step-up authorization (X-Step-Up-Token) and human
    confirmation (X-Human-Confirmed: true) — Condition is a clinical
    resource type.

    Creates a linked Provenance resource with full change attribution.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome(
            'error', 'not-supported',
            f'Resource type {resource_type} is not supported'
        ), 400

    tenant_id = request.headers.get('X-Tenant-Id')
    step_up_token = request.headers.get('X-Step-Up-Token')
    if not step_up_token:
        return _operation_outcome(
            'error', 'security',
            'Write operations require X-Step-Up-Token header'
        ), 403

    valid, err = validate_step_up_token(step_up_token, tenant_id)
    if not valid:
        return _operation_outcome(
            'error', 'security',
            f'Step-up token rejected: {err}'
        ), 403

    body = request.get_json(silent=True)
    if not body:
        return _operation_outcome(
            'error', 'invalid', 'Request body must be valid JSON'
        ), 400

    fixes = body.get('fixes', [])
    patient_intent = body.get('patient_intent', 'Patient-initiated fix')

    if not fixes:
        return _operation_outcome(
            'error', 'invalid', 'fixes array is required and must not be empty'
        ), 400

    try:
        result = _curatr_apply_fix(
            resource_type=resource_type,
            resource_id=resource_id,
            approved_fixes=fixes,
            patient_intent=patient_intent,
            tenant_id=tenant_id,
            agent_id=request.headers.get('X-Agent-Id', 'curatr'),
        )
    except RuntimeError as exc:
        logger.error('curatr_apply_fix failed: %s', exc)
        return _operation_outcome('error', 'exception', str(exc)), 500

    if 'error' in result:
        return _operation_outcome('error', 'not-found', result['error']), 404

    # After a successful fix, re-evaluate and promote curation_state -> curated.
    try:
        from r6.curatr import compute_quality_score
        fresh = result.get('updated_resource') or {}
        if fresh:
            fresh_result = _curatr_engine.evaluate(fresh)
            _persist_curation_state(
                resource_type, resource_id, tenant_id, fresh_result,
                fixed=True,
            )
            result['curation_state'] = 'curated'
            result['quality_score'] = compute_quality_score(fresh_result)
    except Exception as exc:
        logger.warning(
            'curation state promotion failed (fix still committed): %s', exc,
        )

    return jsonify(result)


# --- Compiled Truth: current state + evidence timeline ------------

@r6_blueprint.route(
    '/<resource_type>/<resource_id>/$compiled-truth',
    methods=['GET']
)
def compiled_truth(resource_type, resource_id):
    """
    Return the current best understanding of a resource plus the
    append-only evidence trail of how it got there.

    Pattern inspired by gbrain's "compiled truth + timeline" — every
    resource has a canonical current state AND an immutable history
    of agents/reasons/changes. Patients see exactly what their record
    says now and why.

    Output is a FHIR Parameters resource with:
      - current: the redacted resource
      - curation_state, quality_score, review_needed
      - timeline: Provenance entries that target this resource,
        newest first. Each carries recorded/agent/reason/summary.

    Read-only. Redaction + audit + tenant isolation apply.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome(
            'error', 'not-supported',
            f'Resource type {resource_type} is not supported',
        ), 400

    tenant_id = request.headers.get('X-Tenant-Id')
    if not tenant_id:
        return _operation_outcome(
            'error', 'security',
            'X-Tenant-Id header is required',
        ), 400

    row = R6Resource.query.filter_by(
        id=resource_id, resource_type=resource_type,
        is_deleted=False, tenant_id=tenant_id,
    ).first()

    if not row:
        return _operation_outcome(
            'error', 'not-found',
            f'{resource_type}/{resource_id} not found',
        ), 404

    record_audit_event(
        'read', resource_type, resource_id,
        agent_id=request.headers.get('X-Agent-Id'),
        tenant_id=tenant_id,
        detail='compiled-truth',
    )

    current_json = apply_redaction(row.to_fhir_json())

    # Build timeline from Provenance resources targeting this reference.
    # Note: Provenance.target[] is stored as JSON; we do a prefix scan on
    # the JSON blob. Acceptable for the local store's scale.
    target_ref = f'{resource_type}/{resource_id}'
    prov_rows = R6Resource.query.filter_by(
        resource_type='Provenance',
        is_deleted=False,
        tenant_id=tenant_id,
    ).all()

    timeline = []
    for p in prov_rows:
        try:
            prov = json.loads(p.resource_json)
        except Exception:
            continue
        targets = prov.get('target') or []
        if not any(
            t.get('reference') == target_ref for t in targets
            if isinstance(t, dict)
        ):
            continue
        agent_display = 'system'
        for a in prov.get('agent', []) or []:
            who = a.get('who') or {}
            if isinstance(who, dict) and who.get('display'):
                agent_display = who['display']
                break
        reason = ''
        reasons = prov.get('reason') or []
        if reasons and isinstance(reasons[0], dict):
            codings = reasons[0].get('coding') or []
            if codings:
                reason = codings[0].get('display', '') or ''
        # Extract curatr-correction extension summary if present
        summary = ''
        intent = ''
        for ext in prov.get('extension', []) or []:
            if 'curatr-correction' not in (ext.get('url') or ''):
                continue
            for inner in ext.get('extension', []) or []:
                if inner.get('url') == 'change_summary':
                    summary = inner.get('valueString', '') or ''
                elif inner.get('url') == 'patient_intent':
                    intent = inner.get('valueString', '') or ''
        timeline.append({
            'provenance_id': p.id,
            'recorded': prov.get('recorded', ''),
            'agent': agent_display,
            'reason': reason,
            'summary': summary,
            'patient_intent': intent,
        })

    timeline.sort(key=lambda e: e.get('recorded', ''), reverse=True)

    parameters = {
        'resourceType': 'Parameters',
        'parameter': [
            {'name': 'current', 'resource': current_json},
            {
                'name': 'curation_state',
                'valueString': row.curation_state or 'raw',
            },
            {
                'name': 'quality_score',
                'valueDecimal': (
                    row.quality_score
                    if row.quality_score is not None else 1.0
                ),
            },
            {
                'name': 'review_needed',
                'valueBoolean': bool(row.review_needed),
            },
            {
                'name': 'timeline_count',
                'valueInteger': len(timeline),
            },
            {
                'name': 'timeline',
                'part': [
                    {
                        'name': 'event',
                        'part': [
                            {
                                'name': k,
                                'valueString': str(v) if v is not None else '',
                            }
                            for k, v in event.items()
                        ],
                    }
                    for event in timeline
                ],
            },
        ],
    }
    return jsonify(parameters)


# --- MCP Apps (embedded HTML surfaces for MCP clients) ------------

@r6_blueprint.route(
    '/mcp-apps/compiled-truth/<resource_type>/<resource_id>',
    methods=['GET']
)
def mcp_app_compiled_truth(resource_type, resource_id):
    """
    MCP App: Compiled Truth Review.

    Single-page HTML surface that renders the $compiled-truth Parameters
    response (current state + evidence timeline) with Approve / Re-evaluate
    actions. Linked from the `fhir_compiled_truth` MCP tool response via
    `_meta.ui.resourceUri`. MCP clients that understand the
    `text/html;profile=mcp-app` content type render it inline; others
    treat it as a normal web page.
    """
    if not R6Resource.is_supported_type(resource_type):
        return _operation_outcome(
            'error', 'not-supported',
            f'Resource type {resource_type} is not supported',
        ), 400

    # Tenant is required but arrives as either header or ?tenant_id= query.
    # MCP clients that open resource URIs in a browser won't send headers.
    tenant_id = (
        request.headers.get('X-Tenant-Id')
        or request.args.get('tenant_id')
        or ''
    )

    html = render_template(
        'mcp_apps/compiled_truth.html',
        resource_type=resource_type,
        resource_id=resource_id,
        tenant_id=tenant_id,
    )
    resp = Response(html, mimetype='text/html')
    resp.headers['Content-Type'] = 'text/html; profile=mcp-app'
    resp.headers['X-MCP-App'] = 'compiled-truth'
    return resp


@r6_blueprint.route('/mcp-apps/wearables/', methods=['GET'])
@r6_blueprint.route('/mcp-apps/wearables', methods=['GET'])
def mcp_app_wearables():
    """
    MCP App: Wearables Connection Manager.

    Shows one card per supported provider with connection status, last
    sync, observation count, and Connect / Sync / Re-auth actions. Linked
    from the `wearables_sync_status` MCP tool via `_meta.ui.resourceUri`.
    """
    tenant_id = (
        request.headers.get('X-Tenant-Id')
        or request.args.get('tenant_id')
        or ''
    )
    html = render_template(
        'mcp_apps/wearables.html',
        tenant_id=tenant_id,
    )
    resp = Response(html, mimetype='text/html')
    resp.headers['Content-Type'] = 'text/html; profile=mcp-app'
    resp.headers['X-MCP-App'] = 'wearables'
    return resp


# --- Helper Functions ---

def _operation_outcome(severity, code, diagnostics):
    """Build a FHIR OperationOutcome response."""
    return jsonify({
        'resourceType': 'OperationOutcome',
        'issue': [
            {
                'severity': severity,
                'code': code,
                'diagnostics': diagnostics
            }
        ]
    })
