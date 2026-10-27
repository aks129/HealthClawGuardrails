"""The public key set an EHR sandbox uses to verify our backend client.

SMART Backend Services: `scripts/ehr_sandbox_pull.py` signs a client
assertion with an ES384 key, and Epic and Oracle fetch this set to verify it.
Only the public half lives here, committed as `deploy/ehr-sandbox-jwks.json`
so every key change is a reviewed PR. The private key never reaches this
service.

Served at a purpose-named root path, registered by `main`. Not
`/.well-known/jwks.json`: this origin is also our OAuth authorization server,
and to a verifier that path means "the AS's signing keys".
"""

import json
from pathlib import Path

from flask import jsonify

JWKS_URL_PATH = '/.well-known/healthclaw-ehr-client-jwks.json'
_JWKS_FILE = Path(__file__).resolve().parent.parent / 'deploy' / 'ehr-sandbox-jwks.json'


def ehr_client_jwks_view():
    """The committed public JWKS, read per request (a few hundred bytes)."""
    return jsonify(json.loads(_JWKS_FILE.read_text()))
