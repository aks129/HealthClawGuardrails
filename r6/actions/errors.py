"""Error-taxonomy codes returned at the action gate. Single source of truth;
API responses and tests both reference these constants (never string literals)."""

PROVIDER_NOT_CONFIGURED = 'provider_not_configured'
CONTACT_NOT_ALLOWLISTED = 'contact_not_allowlisted'
DAILY_CAP_REACHED = 'daily_cap_reached'
PAYLOAD_INVALID = 'payload_invalid'
PROVIDER_ERROR = 'provider_error'
EXTRACTION_AMBIGUOUS = 'extraction_ambiguous'
EMERGENCY_INDICATED = 'emergency_indicated'
STALE_SOURCE_DATA = 'stale_source_data'

ALL = (
    PROVIDER_NOT_CONFIGURED, CONTACT_NOT_ALLOWLISTED, DAILY_CAP_REACHED,
    PAYLOAD_INVALID, PROVIDER_ERROR, EXTRACTION_AMBIGUOUS,
    EMERGENCY_INDICATED, STALE_SOURCE_DATA,
)


# ---------------------------------------------------------------------------
# The payload seal, closed as a class (#620)
# ---------------------------------------------------------------------------

#: Fixed text, never built from the exception: PayloadSealed's message names
#: the action id, and the reflection rule keeps caller-adjacent text off the
#: wire (docs/agent-task-guide.md section 2).
SEALED_MESSAGE = ('This action has already been approved. Its payload is '
                  'sealed and cannot change; propose a new action instead.')


def register_error_handlers(app):
    """PayloadSealed answers 409 wherever it is raised.

    #566 caught it at the one review route that had produced a 500; any
    other raise site (a future writer, another route, a background job)
    still answered an unhandled 500. One handler closes the class.
    """
    import logging

    from flask import jsonify

    from r6.actions.models import PayloadSealed

    logger = logging.getLogger(__name__)

    def _render_payload_sealed(exc):
        logger.info('payload seal refused a write: %s', exc)
        return jsonify({'error': SEALED_MESSAGE}), 409

    app.register_error_handler(PayloadSealed, _render_payload_sealed)

