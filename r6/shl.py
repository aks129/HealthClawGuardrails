"""SMART Health Link crypto and payload, U flag (direct file).

Spec: https://docs.smarthealthit.org/smart-health-links/spec/ . A file is a
JWE in compact serialization with `alg` "dir" and `enc` "A256GCM": the
32-byte link key encrypts the content directly, a fresh 12-byte IV per
file, and the additional authenticated data is the ASCII of the protected
header segment. Every segment is base64url without padding.

No `zip`. The spec allows `zip: DEF`, and the TypeScript `shl_generate`
(services/agent-orchestrator) sets it, but a receiver that does not inflate
(RX Genie's prototype/src/shl.rs) cannot read such a file. Uncompressed is
what every receiver reads.

This module is pure: no Flask, no database, no logging. r6/shl_links.py
stores what `encrypt` returns; the key never reaches the store.
"""

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: The protected header every file carries, serialised once.
HEADER = {'alg': 'dir', 'enc': 'A256GCM', 'cty': 'application/fhir+json'}

#: Spec limits on the payload.
MAX_URL_CHARS = 128
MAX_LABEL_CHARS = 80


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def b64url_decode(text):
    if not isinstance(text, str):
        raise ValueError('not base64url')
    try:
        return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    except (ValueError, TypeError) as e:
        raise ValueError('not base64url') from e


def _compact(obj):
    return json.dumps(obj, separators=(',', ':')).encode('utf-8')


def new_key():
    """A fresh 32-byte link key."""
    return os.urandom(32)


def encrypt(key, plaintext, iv=None):
    """Compact JWE of `plaintext` (bytes) under the 32-byte `key`.

    `iv` is for tests only; a caller never passes one, so every file gets a
    fresh random IV (the spec's SHALL when a key is reused)."""
    if len(key) != 32:
        raise ValueError('key must be 32 bytes')
    iv = os.urandom(12) if iv is None else iv
    if len(iv) != 12:
        raise ValueError('iv must be 12 bytes')
    protected = b64url(_compact(HEADER))
    sealed = AESGCM(key).encrypt(iv, plaintext, protected.encode('ascii'))
    ct, tag = sealed[:-16], sealed[-16:]
    return f'{protected}..{b64url(iv)}.{b64url(ct)}.{b64url(tag)}'


def decrypt(key, jwe):
    """Plaintext bytes of a compact JWE, read the way a receiver reads it:
    ciphertext and tag joined, AAD the header segment as sent. Raises
    ValueError on anything else."""
    parts = jwe.strip().split('.') if isinstance(jwe, str) else []
    if len(parts) != 5 or parts[1] != '':
        raise ValueError('not a compact JWE with alg dir')
    header = json.loads(b64url_decode(parts[0]))
    if header.get('alg') != 'dir' or header.get('enc') != 'A256GCM':
        raise ValueError('unsupported JWE algorithm')
    if 'zip' in header:
        raise ValueError('compressed files are not produced or read here')
    iv = b64url_decode(parts[2])
    sealed = b64url_decode(parts[3]) + b64url_decode(parts[4])
    try:
        return AESGCM(key).decrypt(iv, sealed, parts[0].encode('ascii'))
    except Exception as e:  # cryptography raises InvalidTag
        raise ValueError('decryption failed') from e


def build_shlink(url, key, exp, label=None):
    """`shlink:/` + base64url of the payload: url, key, flag U, exp, label.

    `exp` is epoch seconds. The url must be https and at most 128
    characters; the label at most 80."""
    if not isinstance(url, str) or not url.startswith('https://'):
        raise ValueError('SHL url must be https')
    if len(url) > MAX_URL_CHARS:
        raise ValueError('SHL url exceeds 128 characters')
    if len(key) != 32:
        raise ValueError('key must be 32 bytes')
    payload = {'url': url, 'key': b64url(key), 'flag': 'U', 'exp': int(exp)}
    if label is not None:
        if len(label) > MAX_LABEL_CHARS:
            raise ValueError('label exceeds 80 characters')
        payload['label'] = label
    return 'shlink:/' + b64url(_compact(payload))


def parse_shlink(shlink):
    """The payload dict of a bare or viewer-prefixed shlink; ValueError if it
    has no url or a key that is not 32 bytes."""
    if not isinstance(shlink, str) or 'shlink:/' not in shlink:
        raise ValueError('not a shlink')
    payload = json.loads(b64url_decode(shlink.rsplit('shlink:/', 1)[1]))
    if not isinstance(payload, dict) or not isinstance(payload.get('url'), str):
        raise ValueError('shlink payload has no url')
    if len(b64url_decode(payload.get('key'))) != 32:
        raise ValueError('shlink key must be 32 bytes')
    return payload
