#!/usr/bin/env python3
"""Pull one test patient from a public vendor FHIR sandbox as a test bundle.

Real vendor-shaped records, for testing HealthClaw and CareAgents: one
patient from an open (unauthenticated) R4 sandbox, as a FHIR R4 `collection`
Bundle of the check-in types (Patient, problem-list Condition,
AllergyIntolerance, MedicationRequest, Immunization, and Coverage if the
sandbox has any), plus lab Observations with --labs.

Every resource is tagged with the HL7 v3 ActReason HTEST marker on
meta.tag, so scripts/ktc_shl_link.py accepts the bundle and the engine
treats it as test data. `contained` resources are dropped (Oracle serves
an untagged PractitionerRole inside AllergyIntolerance, which the link
gate refuses), with any `#id` reference to them; an element that leaves
empty goes too. Nothing else is changed: the vendor's own `display` and
`text` stay exactly as served, because the point is to test our redaction
against them.

Sources:
  oracle  Oracle Health (Cerner) open sandbox, tenant ec2458f2-... as
          documented in Oracle's R4 overview. Unbounded Condition and
          Immunization searches time out there, so every search is
          `_count=20` and Condition is `category=problem-list-item`; a 504
          is retried once. Default patient 12742400 (12724066 504s on
          Condition and Immunization even with the filters).
  smart   SMART Health IT open R4 sandbox (Synthea). Its Conditions carry
          no category, so Condition is not filtered by one there.

Each type is one search page, cut to 20 resources: a sandbox that ignores
`_count` (Oracle answered 1503 Conditions to `_count=20`) cannot outgrow
the engine's 500-entry ingest. A type whose search fails twice is left out
and reported; a missing Patient fails the run.

--ingest posts the bundle to a running engine's
POST /r6/fhir/internal/ingest-bundle for --tenant, with the internal secret
from INTERNAL_TOKEN_MINT_SECRET (never a flag, so it stays out of shell
history). The tenant must match ^sandbox-(oracle|smart)-[a-z0-9-]+$ and the
engine URL must be https (http only for localhost or 127.0.0.1), both
checked before any request.

No request follows a redirect, and env proxies and CA bundles are ignored.

Public sandbox patients are synthetic, but this treats them as data
anyway: it prints counts per type and the output path, never a resource.

Exit status: 0 written (and ingested); 1 no Patient or the ingest failed;
3 written, but a type's search failed twice and it is left out.

Usage:
  uv run python scripts/sandbox_test_patient.py --source oracle --out o.json
  uv run python scripts/sandbox_test_patient.py --source smart --labs \\
      --out s.json --ingest http://localhost:5099 --tenant sandbox-smart-test
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SOURCES = {
    'oracle': {
        'base': 'https://fhir-open.cerner.com/r4/'
                'ec2458f2-1e24-41c8-b71b-0e701af7583d',
        'patient': '12742400',
        'condition': {'category': 'problem-list-item'},
    },
    'smart': {
        'base': 'https://r4.smarthealthit.org',
        'patient': 'bb7fb8e1-1a76-4328-92ea-3852f34846e6',
        'condition': {},
    },
}

#: The check-in types after Patient, searched by `patient`.
SEARCH_TYPES = ('Condition', 'AllergyIntolerance', 'MedicationRequest',
                'Immunization', 'Coverage')

#: Results kept per type, and the `_count` asked for.
PER_TYPE = 20

HTEST = {'system': 'http://terminology.hl7.org/CodeSystem/v3-ActReason',
         'code': 'HTEST', 'display': 'test health data'}

TIMEOUT_SECONDS = 90

#: --ingest only into a sandbox tenant named for one of these sources.
#: A contained type is printed only when it looks like a FHIR type name.
TYPE_LABEL_RE = re.compile(r'\A[A-Za-z]{1,64}\Z')

TENANT_RE = re.compile(r'\Asandbox-(oracle|smart)-[a-z0-9-]+\Z')
LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1'})


class SandboxError(RuntimeError):
    """The sandbox did not give us the patient."""


def tag_test_data(resource):
    """Add the HTEST marker to meta.tag, once. Nothing else changes."""
    meta = resource.setdefault('meta', {})
    tags = meta.setdefault('tag', [])
    if not any(t.get('system') == HTEST['system']
               and t.get('code') == HTEST['code'] for t in tags):
        tags.append(dict(HTEST))
    return resource


def _drop_refs(value, refs):
    """Remove each `reference` in `refs` below `value`, and any dict or
    list left empty by that. True when `value` itself is now empty."""
    if isinstance(value, dict):
        if value.get('reference') in refs:
            del value['reference']
        for key in list(value):
            if isinstance(value[key], (dict, list)) \
                    and _drop_refs(value[key], refs):
                del value[key]
        return not value
    if isinstance(value, list):
        value[:] = [v for v in value
                    if not (isinstance(v, (dict, list)) and _drop_refs(v, refs))]
        return not value
    return False


def drop_contained(resource, dropped):
    """Drop `contained` from `resource`, and every `#id` reference to what
    it held. `dropped` (a Counter) counts the contained ones by type."""
    contained = resource.pop('contained', None)
    if not isinstance(contained, list):
        return resource
    refs = set()
    for c in contained:
        rtype = c.get('resourceType') if isinstance(c, dict) else None
        dropped[rtype if isinstance(rtype, str)
                and TYPE_LABEL_RE.match(rtype) else 'other'] += 1
        if isinstance(c, dict) and isinstance(c.get('id'), str):
            refs.add('#' + c['id'])
    for key in list(resource):
        if isinstance(resource[key], (dict, list)) \
                and _drop_refs(resource[key], refs):
            del resource[key]
    return resource


def new_session():
    import requests

    session = requests.Session()
    session.trust_env = False   # no env proxy, CA bundle or .netrc
    return session


def check_engine_url(url):
    """True for https, or http to this machine: the secret rides along."""
    try:
        got = urlsplit(url)
        host = (got.hostname or '').lower()
    except ValueError:
        return False
    return bool(host) and (got.scheme == 'https' or (
        got.scheme == 'http' and host in LOCAL_HOSTS))


def _get(session, url, params=None):
    """GET with one retry on a 504 or a timeout. The JSON body, or None
    after two failures (or any other non-200, or a body that is not JSON)."""
    for _ in range(2):
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT_SECONDS,
                               headers={'Accept': 'application/fhir+json'},
                               allow_redirects=False)
        except OSError:  # requests' Timeout and ConnectionError
            continue
        if resp.status_code == 504:
            continue
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None
    return None


def _search(session, base, rtype, params):
    body = _get(session, f'{base}/{rtype}', params)
    if not isinstance(body, dict) or body.get('resourceType') != 'Bundle':
        return None
    found = [e.get('resource') for e in body.get('entry') or []
             if isinstance(e, dict)]
    # A searchset may carry an OperationOutcome or an _include'd resource.
    return [r for r in found
            if isinstance(r, dict) and r.get('resourceType') == rtype][:PER_TYPE]


def build_bundle(source, patient_id, *, session=None, labs=False,
                 dropped_contained=None):
    """(bundle, counts): the tagged collection Bundle, and the count kept
    per type (None for a type whose search failed). Contained resources
    dropped are counted by type into `dropped_contained`, if given."""
    if dropped_contained is None:
        dropped_contained = Counter()
    if session is None:
        session = new_session()
    cfg = SOURCES[source]
    base = cfg['base']

    patient = _get(session, f'{base}/Patient/{patient_id}')
    if not isinstance(patient, dict) or patient.get('resourceType') != 'Patient':
        raise SandboxError('the sandbox did not return the Patient')
    resources = [patient]
    counts = {'Patient': 1}

    searches = [(t, dict(cfg['condition']) if t == 'Condition' else {})
                for t in SEARCH_TYPES]
    if labs:
        searches.append(('Observation', {'category': 'laboratory'}))
    for rtype, extra in searches:
        params = {'patient': patient_id, **extra, '_count': str(PER_TYPE)}
        found = _search(session, base, rtype, params)
        counts[rtype] = None if found is None else len(found)
        resources.extend(found or [])

    entries = [{'fullUrl': f'{base}/{r["resourceType"]}/{r.get("id")}',
                'resource': tag_test_data(
                    drop_contained(r, dropped_contained))}
               for r in resources]
    bundle = {
        'resourceType': 'Bundle',
        'type': 'collection',
        'meta': {'tag': [dict(HTEST)]},
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'entry': entries,
    }
    return bundle, counts


def ingest(bundle, engine, tenant, secret):
    """POST the bundle to the engine's ingest-bundle; its JSON answer. A
    redirect is not followed, so the secret reaches no other host."""
    if not check_engine_url(engine):
        raise ValueError('engine url must be https (http only for localhost)')
    resp = new_session().post(
        f'{engine.rstrip("/")}/r6/fhir/internal/ingest-bundle',
        json={'bundle': bundle}, timeout=120, allow_redirects=False,
        headers={'X-Tenant-Id': tenant, 'X-Internal-Secret': secret})
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--source', choices=sorted(SOURCES), required=True)
    parser.add_argument('--patient',
                        help="sandbox patient id (default: the source's)")
    parser.add_argument('--labs', action='store_true',
                        help='also pull lab Observations')
    parser.add_argument('--out', required=True, help='bundle file to write')
    parser.add_argument('--ingest', metavar='ENGINE_URL',
                        help='also ingest into this engine, e.g. '
                             'http://localhost:5099')
    parser.add_argument('--tenant', help='tenant id for --ingest')
    args = parser.parse_args(argv)

    secret = os.environ.get('INTERNAL_TOKEN_MINT_SECRET', '')
    if args.ingest and not args.tenant:
        parser.error('--ingest needs --tenant')
    if args.ingest and not TENANT_RE.match(args.tenant):
        parser.error('--tenant must match ^sandbox-(oracle|smart)-[a-z0-9-]+$')
    if args.ingest and not check_engine_url(args.ingest):
        parser.error('--ingest must be https (http only for localhost)')

    patient_id = args.patient or SOURCES[args.source]['patient']
    try:
        dropped = Counter()
        bundle, counts = build_bundle(args.source, patient_id, labs=args.labs,
                                      dropped_contained=dropped)
    except SandboxError as e:
        print(f'{args.source}: {e}', file=sys.stderr)
        return 1

    Path(args.out).write_text(json.dumps(bundle, indent=2), encoding='utf-8')
    for rtype, n in counts.items():
        print(f'  {rtype:<20} {"search failed, left out" if n is None else n}')
    for rtype, n in sorted(dropped.items()):
        print(f'  contained {rtype} dropped: {n}')
    print(f'{len(bundle["entry"])} resources -> {args.out}')

    if args.ingest:
        status, body = ingest(bundle, args.ingest, args.tenant, secret)
        # The answer carries counts and stable error codes, never content.
        print(f'ingest: HTTP {status} ingested={body.get("ingested")} '
              f'skipped={body.get("skipped")} failed={body.get("failed")}')
        for err in body.get('errors') or []:
            print(f'  entry {err.get("index")}: {err.get("resourceType")} '
                  f'{err.get("code")}')
        if status != 200:
            return 1
    failed = [t for t, n in counts.items() if n is None]
    return 3 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
