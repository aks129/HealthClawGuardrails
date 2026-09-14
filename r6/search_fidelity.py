"""The search error-fidelity contract, as one module both search paths use.

What an agent is told when a search carries something this server does not
support: an unsupported modifier is always refused, an unknown parameter is
refused under `Prefer: handling=strict` and otherwise ignored, reported in a
warning entry, kept out of the self link, and noted in the audit row. The
same contract on the local path and on the upstream-proxy path (#498): in
proxy mode the query used to be forwarded whole, so the caller got the
upstream's 404 or 502 instead of the corrective message this layer
promises, and nothing was audited as ignored.

Query keys are untrusted input. Only the locally defined names below may be
quoted back in a response or an audit row; everything else gets a generic
corrective message (docs/agent-task-guide.md §2).
"""

from r6.models import AuditEventRecord

# Local-search contract: discovery, validation, corrective messages, and self
# links all derive from this ordered registry.
_SEARCH_PARAMETER_SPECS = (
    {'name': 'patient', 'type': 'reference',
     'documentation': 'Filter by subject.reference (Patient/{id})'},
    {'name': 'code', 'type': 'token',
     'documentation': 'Filter by code.coding[].code (JSON string match)'},
    {'name': 'status', 'type': 'token',
     'documentation': 'Filter by status field'},
    {'name': '_lastUpdated', 'type': 'date',
     'documentation': 'Filter by last updated (ge/le/gt/lt prefix)'},
    {'name': '_count', 'type': 'number',
     'documentation': 'Max results (0-200)'},
    {'name': '_sort', 'type': 'string',
     'documentation': '_lastUpdated or -_lastUpdated'},
    {'name': '_summary', 'type': 'token',
     'documentation': 'count'},
    {'name': 'context-id', 'type': 'token',
     'documentation': 'Filter by local context envelope'},
    {'name': '_id', 'type': 'token',
     'documentation': 'Filter by resource id'},
)
_SUPPORTED_SEARCH_PARAMS = frozenset(
    spec['name'] for spec in _SEARCH_PARAMETER_SPECS)
_SUPPORTED_PARAMS_TEXT = ', '.join(
    spec['name'] for spec in _SEARCH_PARAMETER_SPECS)

# Query keys are untrusted input too. Only these locally defined semantic
# aliases may be named in responses or audit evidence; every other unsupported
# key gets a generic corrective message.
_SAFE_UNSUPPORTED_SEARCH_KEYS = frozenset({'date', 'datetime'})
_SAFE_MODIFIER_TOKENS = frozenset({
    'above', 'below', 'contains', 'exact', 'identifier', 'in', 'iterate',
    'missing', 'not', 'not-in', 'of-type', 'text', 'type',
    # Fixed synthetic token used by the public issue contract.
    'frobnicate',
})


def safe_unsupported_key(key):
    if key in _SAFE_UNSUPPORTED_SEARCH_KEYS:
        return key
    if ':' in key:
        base, modifier = key.split(':', 1)
        if (base in _SUPPORTED_SEARCH_PARAMS
                and modifier in _SAFE_MODIFIER_TOKENS):
            return key
    return None


def unsupported_input_text(kind, key):
    safe_key = safe_unsupported_key(key)
    return f'{kind}: {safe_key}' if safe_key else kind


def error_fidelity_outcome(severity, code, text):
    """Build an OperationOutcome shaped for the error-fidelity contract.

    Unlike _operation_outcome (which uses `diagnostics`), the failure-path
    contract requires `details.text` and an issue carrying nothing else, so a
    consuming agent gets a machine-checkable, corrective message.
    """
    return {
        'resourceType': 'OperationOutcome',
        'issue': [{'severity': severity, 'code': code,
                   'details': {'text': text}}],
    }


def _lenient_search_warning_entries(ignored_params, supported_params_text):
    """Build bounded, value-free warnings for ignored local search keys."""
    safe_ignored = sorted({key for key in ignored_params
                           if safe_unsupported_key(key)})
    has_unnamed = any(not safe_unsupported_key(key)
                      for key in ignored_params)
    warning_keys = [*safe_ignored]
    if has_unnamed:
        warning_keys.append(None)

    entries = []
    for ignored in warning_keys:
        ignored_text = ('Unknown parameter' if ignored is None else
                        f'Unknown parameter: {ignored}')
        entries.append({
            'search': {'mode': 'outcome'},
            'resource': error_fidelity_outcome(
                'warning', 'not-supported',
                f'{ignored_text}. '
                f'Supported parameters: {supported_params_text}.',
            ),
        })
    return entries, safe_ignored, has_unnamed




def classify_search_args(keys, supported):
    """Split query keys into (modifier keys, unknown keys) against `supported`."""
    keys = list(keys)
    modifier_keys = [k for k in keys if ':' in k]
    ignored_params = [k for k in keys if ':' not in k and k not in supported]
    return modifier_keys, ignored_params


def lenient_search_warnings(ignored_params, supported_params_text):
    """Everything a lenient search reports about the keys it ignored.

    Returns (warning entries, audit note, audit outcome code). The allowlist
    has two unknown semantic aliases, plus at most one generic warning for
    every other key, which caps attacker-controlled warning growth at three
    entries regardless of query size. All three values are empty/None when
    nothing was ignored, so a caller can use them unconditionally.
    """
    if not ignored_params:
        return [], '', None
    entries, safe_ignored, has_unnamed = _lenient_search_warning_entries(
        ignored_params, supported_params_text)
    count = len(ignored_params)
    audit_note = ('; unsupported search parameter ignored' if count == 1 else
                  f'; {count} unsupported search parameters ignored')
    return entries, audit_note, AuditEventRecord.ignored_parameters_outcome_code(
        safe_ignored, has_unnamed)
