"""The public key set an EHR sandbox fetches to verify our client assertion.

Epic and Oracle register a backend client against a JWK Set URL and fetch it
on every token request. Only the public half is published; the private key
lives where the pull script runs and never on this service. The path is
purpose-named because this origin is also our OAuth authorization server,
and to a verifier `/.well-known/jwks.json` would mean "AS signing keys".
"""

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

JWKS_PATH = '/.well-known/healthclaw-ehr-client-jwks.json'
COMMITTED = Path(__file__).resolve().parent.parent / 'deploy' / 'ehr-sandbox-jwks.json'
# RFC 7518 §6.2.2 and §6.3.2: the members that only a private key carries.
PRIVATE_MEMBERS = {'d', 'p', 'q', 'dp', 'dq', 'qi', 'oth', 'k'}


def _walk_keys(obj):
    """Every member name anywhere in the document, nested ones included."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def test_served_without_any_credential(client):
    resp = client.get(JWKS_PATH)

    assert resp.status_code == 200
    assert resp.mimetype == 'application/json'
    assert resp.get_json()['keys']


def test_serves_the_committed_file(client):
    assert client.get(JWKS_PATH).get_json() == json.loads(COMMITTED.read_text())


def test_every_key_is_an_es384_signing_key_with_a_kid(client):
    keys = client.get(JWKS_PATH).get_json()['keys']

    for key in keys:
        assert key['kty'] == 'EC'
        assert key['crv'] == 'P-384'
        assert key['alg'] == 'ES384'
        assert key['use'] == 'sig'
        assert key['kid'].startswith('healthclaw-ehr-sandbox-es384-')
    assert len({k['kid'] for k in keys}) == len(keys)
    assert 'healthclaw-ehr-sandbox-es384-1' in {k['kid'] for k in keys}


def test_no_private_member_anywhere(client):
    served = client.get(JWKS_PATH).get_json()

    for doc in (served, json.loads(COMMITTED.read_text())):
        assert not PRIVATE_MEMBERS & set(_walk_keys(doc))


def test_every_key_is_a_point_on_p384(client):
    """A typo in x or y would publish a key no vendor can verify against."""
    import base64

    def _int(b64url):
        return int.from_bytes(
            base64.urlsafe_b64decode(b64url + '=' * (-len(b64url) % 4)), 'big')

    for key in client.get(JWKS_PATH).get_json()['keys']:
        # public_key() raises ValueError for a point off the curve.
        ec.EllipticCurvePublicNumbers(
            _int(key['x']), _int(key['y']), ec.SECP384R1()).public_key()


def test_not_published_at_the_authorization_server_jwks_path(client):
    assert client.get('/.well-known/jwks.json').status_code == 404


def test_read_only(client):
    assert client.post(JWKS_PATH).status_code == 405
