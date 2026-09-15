"""curatr-fix rail — the ActionExecutor for kind 'curatr-fix' (#413).

A Curatr data-quality fix reaches the record the same way a phone call
reaches a pharmacy: proposed by an agent, shown to a human, approved with
the action-bound credential, then executed once. The `$curatr-apply-fix`
route and its production gate are untouched; this rail is the path that is
reachable in production, and only once the operator sets
CURATR_FIX_RAIL_ENABLED.

Payload shape (validated at propose, re-validated at execute):

    {"to": "Condition/<id>",                 # the record, as the human sees it
     "body": "<what the fix does, in words>",
     "curatr_fix": {"resource_type": "Condition",
                    "resource_id": "<id>",
                    "record_version": 3,       # meta.versionId when proposed
                    "fixes": [{"field_path": "Condition.clinicalStatus.coding[0].code",
                               "new_value": "active"}],
                    "patient_intent": "<the patient's words>"}}

Stale-proposal policy: the proposal names the record version it was made
against. If the record has moved on by the time the human's approval
executes, nothing is applied (failed / STALE_SOURCE_DATA) — the approval
was for the record they saw. The compare happens inside apply_fix, on the
same loaded row that would be mutated.

The outcome carries ids, counts and field paths only — never the record.
"""

import os

from r6.actions import errors
from r6.actions.registry import ExecutionResult, register_executor
from r6.curatr import (FIXABLE_ROOTS, FixRefused, _parse_fix, apply_fix,
                       find_fix_provenance)
from r6.resource_ids import _PATH_RESOURCE_ID_PATTERN

FLAG = 'CURATR_FIX_RAIL_ENABLED'
MAX_FIXES = 20


def _spec_errors(payload):
    """Every reason the payload is not a curatr fix, as error codes (all
    PAYLOAD_INVALID — the reasons are for the log, the code is the contract)."""
    body = payload.get('body')
    if not isinstance(body, str) or not body:
        return [errors.PAYLOAD_INVALID]
    spec = payload.get('curatr_fix')
    if not isinstance(spec, dict):
        return [errors.PAYLOAD_INVALID]
    rtype = spec.get('resource_type')
    rid = spec.get('resource_id')
    roots = FIXABLE_ROOTS.get(rtype) if isinstance(rtype, str) else None
    if roots is None:
        return [errors.PAYLOAD_INVALID]
    if not isinstance(rid, str) or not _PATH_RESOURCE_ID_PATTERN.match(rid):
        return [errors.PAYLOAD_INVALID]
    # The label the human approves names the record that gets changed.
    if payload.get('to') != '%s/%s' % (rtype, rid):
        return [errors.PAYLOAD_INVALID]
    version = spec.get('record_version')
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return [errors.PAYLOAD_INVALID]
    intent = spec.get('patient_intent')
    if not isinstance(intent, str) or not intent:
        return [errors.PAYLOAD_INVALID]
    fixes = spec.get('fixes')
    if not isinstance(fixes, list) or not fixes or len(fixes) > MAX_FIXES:
        return [errors.PAYLOAD_INVALID]
    # The same grammar the executor applies, minus what needs the record
    # (index bounds, linkage). A fix the evaluator could not have proposed
    # is refused here, before a person is asked to approve it.
    for n, fix in enumerate(fixes, start=1):
        try:
            _parse_fix(n, rtype, fix)
        except FixRefused:
            return [errors.PAYLOAD_INVALID]
    return []


class CuratrFixExecutor:
    kind = 'curatr-fix'
    # No provider; the flag is the switch that keeps the rail dark until an
    # operator turns it on. Unset = fail loud, never a silent no-op.
    required_env = (FLAG,)

    def validate(self, payload):
        return _spec_errors(payload if isinstance(payload, dict) else {})

    def execute(self, action):
        # (1) The switch, before anything payload-specific.
        if not os.environ.get(FLAG):
            return ExecutionResult(status='failed',
                                   error=errors.PROVIDER_NOT_CONFIGURED)
        # (2) The payload was validated at propose and sealed at approval;
        # re-check anyway so a row written any other way cannot reach the
        # record.
        payload = action.payload or {}
        if _spec_errors(payload):
            return ExecutionResult(status='failed', error=errors.PAYLOAD_INVALID)
        spec = payload['curatr_fix']
        ref = payload['to']
        version = spec['record_version']

        # (3) Durable evidence first. If an earlier attempt for this action
        # committed — and then the action row could not be updated, or the
        # process died — the Provenance naming this action is still there.
        # Report that outcome; never apply the fix a second time.
        earlier = find_fix_provenance(action.tenant_id, action.id)
        if earlier is not None:
            return self._completed(ref, earlier, version, recovered=True)

        # (4) One call mutates, audits and writes Provenance in one
        # transaction — or refuses whole. An exception must never read as
        # success; its text may name the record, so only its class is kept.
        try:
            result = apply_fix(
                spec['resource_type'], spec['resource_id'], spec['fixes'],
                spec['patient_intent'], action.tenant_id,
                agent_id='curatr-fix', expected_version=version,
                action_ref=action.id)
        except Exception as exc:  # noqa: BLE001 — fail loud, never fake success
            return ExecutionResult(status='failed', error=errors.PROVIDER_ERROR,
                                   outcome={'resource': ref,
                                            'detail': type(exc).__name__})

        if result.get('stale'):
            return ExecutionResult(
                status='failed', error=errors.STALE_SOURCE_DATA,
                outcome={'resource': ref, 'proposed_version': version,
                         'current_version': result.get('current_version')})
        if result.get('refused'):
            return ExecutionResult(
                status='failed', error=errors.PAYLOAD_INVALID,
                outcome={'resource': ref, 'reason': result.get('error')})
        if result.get('error'):
            return ExecutionResult(
                status='failed', error=errors.PROVIDER_ERROR,
                outcome={'resource': ref, 'reason': result.get('error')})

        # (5) Success: the Provenance id is the provider ref. Field paths
        # only — the changed record stays in the record store.
        return self._completed(ref, result.get('provenance') or {}, version)

    @staticmethod
    def _completed(ref, provenance, version, recovered=False):
        """The completed outcome, read from the committed Provenance so a
        recovered attempt and a fresh one report the same facts."""
        ext = {}
        for outer in (provenance.get('extension') or []):
            for inner in (outer.get('extension') or []):
                ext[inner.get('url')] = inner
        summary = (ext.get('change_summary') or {}).get('valueString') or ''
        outcome = {'resource': ref,
                   'issues_fixed': (ext.get('changes_applied') or {}).get('valueInteger'),
                   'provenance_id': provenance.get('id'),
                   'version_before': version,
                   'version_after': version + 1,
                   'fields': [p[:-len(' updated')] for p in summary.split('; ')
                              if p.endswith(' updated')]}
        if recovered:
            outcome['recovered'] = True
        return ExecutionResult(status='completed',
                               provider_ref=provenance.get('id'),
                               outcome=outcome)

    def reconcile(self, action):
        # There is no provider to ask, but there is durable evidence: the
        # Provenance that names this action. Found: the fix was applied,
        # once. Not found: nothing committed, and this is not the place to
        # apply it — needs_review, never a second execution.
        payload = action.payload or {}
        spec = payload.get('curatr_fix') or {}
        earlier = find_fix_provenance(action.tenant_id, action.id)
        if earlier is not None:
            return self._completed(payload.get('to'), earlier,
                                   spec.get('record_version') or 0,
                                   recovered=True)
        return ExecutionResult(
            status='needs_review',
            outcome={'reason': 'no committed Provenance names this action; '
                               'the fix was not applied'})


def register():
    register_executor(CuratrFixExecutor())


register()
