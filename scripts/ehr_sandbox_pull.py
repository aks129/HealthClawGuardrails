#!/usr/bin/env python3
"""
scripts/ehr_sandbox_pull.py

Pull one Epic or Oracle *sandbox* patient into a sandbox tenant, over SMART
Backend Services (client_credentials with a signed ES384 assertion).

    uv run python scripts/ehr_sandbox_pull.py --vendor epic \\
        --patient <sandbox patient id> --tenant sandbox-epic-<name>

It gets a token, reads the patient and a fixed set of searches, keeps only
the allowlisted types, marks every entry HTEST, writes a transaction Bundle
to exports/ (gitignored) and POSTs it to the engine's /internal/seed. It
prints counts and types only. Runbook: docs/runbooks/ehr-sandbox-pull.md.

Guardrails (design 2026-10-26):
- The vendor URLs are exact strings, never read from env:
  authorization.cerner.com also serves production tenants, and production
  differs from the code sandbox by one token. The session ignores env
  proxies and CA bundles; no request follows a redirect; a paging `next`
  link is followed only under the same FHIR base.
- The type allowlist here is the only filter: the seed's new-row path does
  not apply the ingest bundle's forbidden types.
- Records are stored as received (meta.source and our record-source tags
  aside). Redaction happens at the engine's exit, not here.
- Never for a production EHR. That is a new design review.

The private key lives only where this runs (default
~/.healthclaw/ehr-sandbox-es384-1.pem, mode 0600). Client ids come from
EPIC_SANDBOX_CLIENT_ID / ORACLE_SANDBOX_CLIENT_ID, else
~/.healthclaw/ehr-sandbox.json. The seed secret comes from
INTERNAL_TOKEN_MINT_SECRET, never argv.
"""

import argparse
import base64
import copy
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from r6.record_source import strip_all  # noqa: E402


@dataclass(frozen=True)
class Vendor:
    name: str
    token_url: str
    fhir_base: str
    scope: str | None   # None: send no scope (Epic)


_ORACLE_TENANT = 'ec2458f2-1e24-41c8-b71b-0e701af7583d'

# Exact strings. Do not build these from env or arguments.
VENDORS = {
    'epic': Vendor(
        name='epic',
        token_url='https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token',
        fhir_base='https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4',
        scope=None,
    ),
    'oracle': Vendor(
        name='oracle',
        token_url=('https://authorization.cerner.com/tenants/'
                   f'{_ORACLE_TENANT}/hosts/fhir-ehr-code.cerner.com/'
                   'protocols/oauth2/profiles/smart-v1/token'),
        fhir_base=f'https://fhir-ehr-code.cerner.com/r4/{_ORACLE_TENANT}',
        # SMART v1 `.read`, for the types the Oracle app is registered for.
        scope=' '.join(f'system/{t}.read' for t in (
            'AllergyIntolerance', 'Condition', 'Coverage', 'Immunization',
            'MedicationRequest', 'Observation', 'Organization', 'Patient')),
    ),
}

ALLOWED_TYPES = frozenset({
    'Patient', 'AllergyIntolerance', 'Condition', 'MedicationRequest',
    'Medication', 'Immunization', 'Coverage', 'Organization', 'Observation',
})

# (type, extra search params) after the Patient read. `_include`d resources
# are filtered and marked like any other entry.
SEARCHES = (
    ('AllergyIntolerance', {}),
    ('Condition', {'category': 'problem-list-item'}),
    ('MedicationRequest', {'_include': 'MedicationRequest:medication'}),
    ('Immunization', {}),
    ('Coverage', {'_include': 'Coverage:payor'}),
    ('Observation', {'category': 'laboratory'}),
    ('Observation', {'category': 'vital-signs'}),
)

TENANT_RE = re.compile(r'\Asandbox-(epic|oracle)-[a-z0-9-]+\Z')
# A dropped type is printed only when it looks like a FHIR type name;
# anything else is upstream text and is counted as 'other'.
TYPE_LABEL_RE = re.compile(r'\A[A-Za-z]{1,64}\Z')
LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1'})
KID = 'healthclaw-ehr-sandbox-es384-1'
DEFAULT_KEY = Path.home() / '.healthclaw' / 'ehr-sandbox-es384-1.pem'
CLIENT_CONFIG = Path.home() / '.healthclaw' / 'ehr-sandbox.json'
CLIENT_ID_ENV = {'epic': 'EPIC_SANDBOX_CLIENT_ID',
                 'oracle': 'ORACLE_SANDBOX_CLIENT_ID'}
SECRET_ENV = 'INTERNAL_TOKEN_MINT_SECRET'
DEFAULT_ENGINE_URL = 'https://app.healthclaw.io'
SEED_PATH = '/r6/fhir/internal/seed'
EXPORTS_DIR = ROOT / 'exports'

HTEST = {'system': 'http://terminology.hl7.org/CodeSystem/v3-ActReason',
         'code': 'HTEST'}
ASSERTION_TTL = 300   # seconds; both vendors cap it at five minutes
MAX_PAGES = 20        # per search
MAX_ENTRIES = 2000    # per search
TIMEOUT = 30
# A sandbox search can be slow: Oracle's problem-list search has taken
# more than 30 s for one patient.
SEARCH_TIMEOUT = 120


class PullError(Exception):
    """A refusal or failure. Its message never carries a response body."""


def now():
    return int(time.time())


def new_session():
    session = requests.Session()
    # No HTTPS_PROXY, REQUESTS_CA_BUNDLE or .netrc: env cannot move traffic.
    session.trust_env = False
    return session


def check_tenant(tenant):
    if not isinstance(tenant, str) or not TENANT_RE.match(tenant):
        raise PullError('tenant must match ^sandbox-(epic|oracle)-[a-z0-9-]+$')


def check_engine_url(url):
    """https, or http only to this machine: the seed secret rides along."""
    try:
        got = urlsplit(url)
        host = (got.hostname or '').lower()
    except ValueError:
        raise PullError('engine url is not a URL') from None
    if not host or not (got.scheme == 'https' or (
            got.scheme == 'http' and host in LOCAL_HOSTS)):
        raise PullError('engine url must be https (http only for localhost)')


def client_id(vendor_name):
    value = os.environ.get(CLIENT_ID_ENV[vendor_name], '').strip()
    if value:
        return value
    try:
        config = json.loads(CLIENT_CONFIG.read_text())
    except (OSError, ValueError):
        config = {}
    value = config.get(f'{vendor_name}_client_id') if isinstance(
        config, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise PullError(f'no {vendor_name} client id: set '
                        f'{CLIENT_ID_ENV[vendor_name]} or {CLIENT_CONFIG}')
    return value.strip()


def load_key(path):
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), None)
    except (OSError, ValueError, TypeError):
        raise PullError('could not load the private key') from None
    if not (isinstance(key, ec.EllipticCurvePrivateKey)
            and isinstance(key.curve, ec.SECP384R1)):
        raise PullError('the private key is not P-384')
    return key


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def client_assertion(vendor, cid, key):
    """An ES384 JWS: header alg/typ/kid only (no jku), aud = token URL."""
    header = {'alg': 'ES384', 'typ': 'JWT', 'kid': KID}
    claims = {'iss': cid, 'sub': cid, 'aud': vendor.token_url,
              'jti': uuid.uuid4().hex, 'exp': now() + ASSERTION_TTL}
    signing_input = '.'.join(
        _b64url(json.dumps(part, separators=(',', ':')).encode())
        for part in (header, claims))
    der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA384()))
    r, s = decode_dss_signature(der)
    return signing_input + '.' + _b64url(r.to_bytes(48, 'big')
                                         + s.to_bytes(48, 'big'))


def get_token(session, vendor, cid, key):
    form = {
        'grant_type': 'client_credentials',
        'client_assertion_type':
            'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
        'client_assertion': client_assertion(vendor, cid, key),
    }
    if vendor.scope:
        form['scope'] = vendor.scope
    resp = session.post(vendor.token_url, data=form,
                        headers={'Accept': 'application/json'},
                        allow_redirects=False, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise PullError(f'token: HTTP {resp.status_code}')
    try:
        token = resp.json().get('access_token')
    except (ValueError, AttributeError):
        token = None
    if not isinstance(token, str) or not token:
        raise PullError('token: no access_token in the answer')
    return token


def _under_base(vendor, url):
    """True only for an https URL on the base's host, under its path. A dot
    segment, or a percent-encoded dot or slash in a segment, is refused
    outright: the string check would pass it and the request (or the
    server) would then leave the base path."""
    base = urlsplit(vendor.fhir_base)
    got = urlsplit(url)
    if any(seg in ('.', '..') or '%2e' in seg.lower() or '%2f' in seg.lower()
           for seg in got.path.split('/')):
        return False
    return (got.scheme == 'https' and got.netloc == base.netloc
            and url.startswith(vendor.fhir_base + '/'))


def get_json(session, vendor, token, url, params=None):
    if not _under_base(vendor, url):
        raise PullError('refused a URL outside the sandbox FHIR base')
    resp = session.get(url, params=params,
                       headers={'Authorization': f'Bearer {token}',
                                'Accept': 'application/fhir+json'},
                       allow_redirects=False, timeout=SEARCH_TIMEOUT)
    if resp.status_code != 200:
        raise PullError(f'GET: HTTP {resp.status_code}')
    try:
        body = resp.json()
    except ValueError:
        raise PullError('GET: answer is not JSON') from None
    if not isinstance(body, dict):
        raise PullError('GET: answer is not a FHIR resource')
    return body


def fetch_patient(session, vendor, token, patient_id):
    return get_json(session, vendor, token,
                    f'{vendor.fhir_base}/Patient/{quote(patient_id, safe="")}')


def _next_link(bundle):
    return next((link.get('url') for link in bundle.get('link') or []
                 if isinstance(link, dict)
                 and link.get('relation') == 'next'), None)


def search(session, vendor, token, rtype, params, truncated=None):
    """Every entry resource across pages, under the page and entry caps.
    A cap that cuts off more data appends (rtype, count) to `truncated`."""
    out = []
    url, query = f'{vendor.fhir_base}/{rtype}', params
    for _ in range(MAX_PAGES):
        bundle = get_json(session, vendor, token, url, query)
        entries = bundle.get('entry') or []
        for i, entry in enumerate(entries):
            res = entry.get('resource') if isinstance(entry, dict) else None
            if isinstance(res, dict):
                out.append(res)
            if len(out) >= MAX_ENTRIES:
                if (i + 1 < len(entries) or _next_link(bundle)) \
                        and truncated is not None:
                    truncated.append((rtype, len(out)))
                return out
        nxt = _next_link(bundle)
        if not nxt:
            return out
        if not _under_base(vendor, nxt):
            raise PullError(f'{rtype}: refused a next link off the FHIR base')
        url, query = nxt, None
    if truncated is not None:
        truncated.append((rtype, len(out)))
    return out


def fetch(session, vendor, token, patient_id, truncated=None):
    resources = [fetch_patient(session, vendor, token, patient_id)]
    for rtype, extra in SEARCHES:
        resources.extend(search(session, vendor, token, rtype,
                                dict(extra, patient=patient_id), truncated))
    return resources


def _mark_test_data(res):
    """HTEST on meta.tag, system and code only. An upstream HTEST coding (in
    either slot, with whatever display it carried) is replaced."""
    meta = res.get('meta')
    if not isinstance(meta, dict):
        meta = res['meta'] = {}
    for slot in ('tag', 'security'):
        codings = meta.get(slot)
        if isinstance(codings, list):
            kept = [c for c in codings if not (
                isinstance(c, dict) and c.get('system') == HTEST['system']
                and c.get('code') == HTEST['code'])]
            if kept:
                meta[slot] = kept
            else:
                meta.pop(slot, None)
    if not isinstance(meta.get('tag'), list):
        meta.pop('tag', None)   # malformed upstream: treat as untagged
    meta.setdefault('tag', []).append(dict(HTEST))


def prepare(resources):
    """Allowlisted types with an id, once each, stripped and marked."""
    return prepare_counted(resources)[0]


def prepare_counted(resources):
    """`prepare`, plus a Counter of what was dropped, by type only."""
    kept, seen, dropped = [], set(), Counter()
    for res in resources:
        rtype = res.get('resourceType') if isinstance(res, dict) else None
        rid = res.get('id') if isinstance(res, dict) else None
        if rtype not in ALLOWED_TYPES or not isinstance(rid, str) or not rid:
            label = rtype if isinstance(rtype, str) \
                and TYPE_LABEL_RE.match(rtype) else 'other'
            dropped[label] += 1
            continue
        if (rtype, rid) in seen:
            continue
        seen.add((rtype, rid))
        res = copy.deepcopy(res)
        strip_all(res)
        _mark_test_data(res)
        kept.append(res)
    return kept, dropped


def transaction_bundle(resources):
    return {
        'resourceType': 'Bundle', 'type': 'transaction',
        'entry': [{'resource': r,
                   'request': {'method': 'PUT',
                               'url': f"{r['resourceType']}/{r['id']}"}}
                  for r in resources],
    }


def write_export(tenant, bundle):
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = EXPORTS_DIR / f'ehr-sandbox-{tenant}-{stamp}.json'
    path.write_text(json.dumps(bundle, indent=2))
    return path


def seed(session, engine_url, tenant, bundle, secret):
    """POST to /internal/seed. Returns created_count, or None when the
    answer has none; nothing else of the answer (it carries a step-up
    token) is read or printed."""
    resp = session.post(engine_url + SEED_PATH,
                        json={'tenant_id': tenant, 'bundle': bundle},
                        headers={'X-Internal-Secret': secret,
                                 'X-Tenant-Id': tenant},
                        allow_redirects=False, timeout=TIMEOUT)
    if resp.status_code != 201:
        raise PullError(f'seed: HTTP {resp.status_code}')
    try:
        count = resp.json().get('created_count')
    except (ValueError, AttributeError):
        count = None
    return count if isinstance(count, int) else None


def _counts(counter):
    return ', '.join(f'{t} {n}' for t, n in sorted(counter.items())) or 'none'


def main(argv=None, *, session=None, seed_session=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[1])
    parser.add_argument('--vendor', required=True, choices=sorted(VENDORS))
    parser.add_argument('--patient', required=True,
                        help='the sandbox patient id')
    parser.add_argument('--tenant', required=True,
                        help='sandbox-(epic|oracle)-<name>')
    parser.add_argument('--key', default=str(DEFAULT_KEY),
                        help='ES384 private key PEM (default: %(default)s)')
    parser.add_argument('--engine-url', default=DEFAULT_ENGINE_URL,
                        help='https, or http for localhost only '
                             '(default: %(default)s)')
    parser.add_argument('--dry-run', action='store_true',
                        help='write the export only; do not seed')
    args = parser.parse_args(argv)

    vendor = VENDORS[args.vendor]
    try:
        check_tenant(args.tenant)
        check_engine_url(args.engine_url)
        cid = client_id(vendor.name)
        secret = os.environ.get(SECRET_ENV, '').strip()
        if not secret and not args.dry_run:
            raise PullError(f'{SECRET_ENV} is not set')
        key = load_key(args.key)
        session = session or new_session()
        token = get_token(session, vendor, cid, key)
        truncated = []
        kept, dropped = prepare_counted(
            fetch(session, vendor, token, args.patient, truncated))
        del token
        bundle = transaction_bundle(kept)
        path = write_export(args.tenant, bundle)
        print(f'{vendor.name}: kept {_counts(Counter(r["resourceType"] for r in kept))}')
        print(f'{vendor.name}: dropped {_counts(dropped)}')
        for rtype, count in truncated:
            print(f'{vendor.name}: {rtype} truncated at {count}')
        print(f'wrote {path.name}')
        if args.dry_run:
            print('dry run: not seeded')
            return 0
        created = seed(seed_session or new_session(),
                       args.engine_url.rstrip('/'), args.tenant, bundle,
                       secret)
        print(f'seeded {args.tenant}: created '
              f'{"unknown" if created is None else created}')
    except PullError as exc:
        print(f'error: {exc}')
        return 1
    except requests.RequestException as exc:
        print(f'error: {type(exc).__name__}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
