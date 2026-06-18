"""FHIRPath evaluation for SDC populate/extract.

Thin wrapper over fhirpathpy. Evaluation failures return None rather than
raising, so a single bad expression in a Questionnaire never aborts the
whole populate/extract run (the caller records an issue instead).
"""

import logging

import fhirpathpy

logger = logging.getLogger(__name__)


def build_context(subject=None, resources=None, extra=None):
    """Build the FHIRPath environment-variable context.

    %patient / %subject resolve to the populate subject; named entries in
    `extra` (e.g. launchContext or variable values) are passed through.
    """
    context = {}
    if subject is not None:
        context['patient'] = subject
        context['subject'] = subject
    if resources:
        context['resources'] = resources
    if extra:
        context.update(extra)
    return context


def evaluate(expression, resource, context=None):
    """Evaluate a FHIRPath expression, returning a scalar, list, or None.

    Returns the single value when the result has one element, the list when
    it has several, and None when empty or on any evaluation error.
    """
    if not expression:
        return None
    try:
        result = fhirpathpy.evaluate(resource or {}, expression, context or {})
    except Exception as exc:  # noqa: BLE001 — never let one expr kill the run
        logger.warning('FHIRPath evaluation failed for %r: %s',
                       expression, type(exc).__name__)
        return None
    if not result:
        return None
    return result[0] if len(result) == 1 else result
