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
# The payload about to execute does not hash to what the human approved
# (#559, human-gate spec 8.3): a writer past the ORM seal, or a confirmation
# minted before digests existed. Never executes.
APPROVED_PAYLOAD_MISMATCH = 'approved_payload_mismatch'

ALL = (
    PROVIDER_NOT_CONFIGURED, CONTACT_NOT_ALLOWLISTED, DAILY_CAP_REACHED,
    PAYLOAD_INVALID, PROVIDER_ERROR, EXTRACTION_AMBIGUOUS,
    EMERGENCY_INDICATED, STALE_SOURCE_DATA, APPROVED_PAYLOAD_MISMATCH,
)
