#!/usr/bin/env python3
"""Make a SMART Health Link for a synthetic bundle on the CMS Kill the
Clipboard test harness.

A Python port of FHIR-IQ/rxgenie prototype/scripts/ktc-shl-link.mjs: the
bundle is encrypted as a compact JWE (alg dir, enc A256GCM, cty
application/fhir+json, AAD the protected header segment) under a fresh
32-byte key, `{"jwe": ...}` is POSTed to the harness, and the U-flag
`shlink:/` it prints names the harness's file URL, the key, an `exp` at
most one hour out and a label.

The crypto and the payload are r6/shl.py's, the engine's own, so this link
and the engine's `deliver: link` are read the same way by a receiver. One
difference from the .mjs: it encodes the payload with standard base64
(`btoa`), the SHL spec and r6/shl.py use base64url, which is what this
prints.

Test data only. The harness is a third-party host, so a bundle is refused
(exit 2, nothing sent) unless it has entries and every entry's resource
carries the HL7 v3 ActReason HTEST marker on meta.tag or meta.security,
matched as r6/ktc.py `is_test_data` matches it. scripts/sandbox_test_patient.py
writes bundles that pass.

Usage:
  uv run python scripts/ktc_shl_link.py bundle.json
  uv run python scripts/ktc_shl_link.py bundle.json --ttl 600 --label "Run 3"

Prints the shlink and the entry count, never the bundle.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r6 import shl  # noqa: E402
from r6.ktc import is_test_data  # noqa: E402

#: The CMS KTC test harness's link store.
HARNESS_URL = 'https://pshd-shl.exe.xyz/shl'

#: The longest a link may live.
MAX_TTL_SECONDS = 3600

#: Fixed words: a receiver may log the label, so it names no person.
DEFAULT_LABEL = 'Synthetic test records'

#: What the harness may answer as a link id: plain URL-safe characters,
#: short enough for the spec's 128-character url.
_LINK_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')


class NotTestData(ValueError):
    """The bundle is not all HTEST-tagged test data."""


def check_test_only(bundle):
    """Raise NotTestData unless `bundle` is a Bundle with at least one entry
    and every entry's resource carries the HTEST marker."""
    if not isinstance(bundle, dict) or bundle.get('resourceType') != 'Bundle':
        raise NotTestData('not a FHIR Bundle')
    entries = bundle.get('entry')
    if not isinstance(entries, list) or not entries:
        raise NotTestData('the bundle has no entries')
    for i, entry in enumerate(entries):
        resource = entry.get('resource') if isinstance(entry, dict) else None
        if not is_test_data(resource):
            raise NotTestData(f'entry {i} is not tagged HTEST')


def post_jwe(jwe, url=HARNESS_URL):
    """POST `{jwe}` to the harness; the link id it answers."""
    import requests

    resp = requests.post(url, json={'jwe': jwe}, timeout=30)
    resp.raise_for_status()
    return resp.json().get('id')


def make_link(bundle, *, ttl=MAX_TTL_SECONDS, label=DEFAULT_LABEL,
              post=None, now=None):
    """The `shlink:/` for `bundle`, stored on the harness by `post`.

    Every check runs before anything is sent."""
    check_test_only(bundle)
    if isinstance(ttl, bool) or not isinstance(ttl, int) \
            or not 0 < ttl <= MAX_TTL_SECONDS:
        raise ValueError(f'ttl must be 1..{MAX_TTL_SECONDS} seconds')
    if len(label) > shl.MAX_LABEL_CHARS:
        raise ValueError(f'label exceeds {shl.MAX_LABEL_CHARS} characters')
    post = post or post_jwe
    key = shl.new_key()
    jwe = shl.encrypt(key, json.dumps(bundle).encode('utf-8'))
    link_id = post(jwe, url=HARNESS_URL)
    if not (isinstance(link_id, str) and _LINK_ID.fullmatch(link_id)):
        raise ValueError('the harness did not answer a link id')
    exp = int(time.time() if now is None else now) + ttl
    return shl.build_shlink(f'{HARNESS_URL}/{link_id}', key, exp, label)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('bundle', help='path to a FHIR Bundle JSON file')
    parser.add_argument('--ttl', type=int, default=MAX_TTL_SECONDS,
                        help=f'seconds until exp, at most {MAX_TTL_SECONDS}')
    parser.add_argument('--label', default=DEFAULT_LABEL)
    args = parser.parse_args(argv)

    try:
        bundle = json.loads(Path(args.bundle).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        print(f'cannot read the bundle: {type(e).__name__}', file=sys.stderr)
        return 2
    try:
        shlink = make_link(bundle, ttl=args.ttl, label=args.label,
                           post=post_jwe)
    except NotTestData as e:
        print(f'refused, nothing sent: {e}', file=sys.stderr)
        return 2
    except ValueError as e:
        print(f'refused: {e}', file=sys.stderr)
        return 2
    except OSError as e:  # requests' errors are OSErrors
        print(f'the harness did not take the file: {type(e).__name__}',
              file=sys.stderr)
        return 1
    print(f'{len(bundle["entry"])} entries stored on {HARNESS_URL}',
          file=sys.stderr)
    print(shlink)
    return 0


if __name__ == '__main__':
    sys.exit(main())
