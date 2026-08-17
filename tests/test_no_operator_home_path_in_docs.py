"""A pasted shell transcript must not carry the operator's account name.

Evidence packs are the most quoted artifacts this repo produces, and they are
built by pasting real terminal output — which is the point. Real terminal
output contains `/Users/<account>/...` and `ls -l` owner columns, and this
repository is public.

The two Wave-1 packs shipped with eight such lines while the PR that added
them stated they had been scanned and contained no real identity. That is the
defect shape the packs themselves are about: a self-assessment substituted for
a check. So the check exists now.

The needle is the *shape* — an absolute home path with a concrete account name
— rather than any particular name. A guard keyed to one person's username
would not fire for the next contributor, and would have to write that username
into a public file to work at all.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories whose contents are published or quoted outward.
_SCANNED_DIRS = ('docs',)

# `/Users/name/` (macOS) and `/home/name/` (Linux). The placeholder forms a
# redaction should use are allowed through, and so is the literal `~`.
_HOME_PATH = re.compile(r'/(?:Users|home)/(?!<)([A-Za-z0-9._-]+)/')

# The other half of the same leak: an `ls -l` owner column. Four of the eight
# shipped lines were this shape, and a home-path regex does not see it.
#   drwxr-xr-x@  2 someone  staff   64 Aug 16 17:16 .
_LS_OWNER = re.compile(
    r'^[-dlbcps][rwxsStT-]{9}[@+.]?\s+\d+\s+(?!<)([A-Za-z0-9._-]+)\s+\S+\s+\d')

# Redaction placeholders and the shared-runner accounts that appear in pasted
# CI output. `runner` and `ubuntu` are GitHub Actions; they name no person.
_ALLOWED_ACCOUNTS = frozenset({
    'user', 'username', 'you', 'me', 'youruser', 'your-user',
    'runner', 'ubuntu', 'root', 'home',
})


def _published_markdown() -> list[Path]:
    paths: list[Path] = []
    for directory in _SCANNED_DIRS:
        paths.extend(sorted((REPO_ROOT / directory).rglob('*.md')))
    return paths


def test_the_scan_reaches_the_evidence_packs():
    """Guard the guard: a scan that silently covers nothing passes forever."""
    scanned = _published_markdown()
    assert len(scanned) >= 20, (
        'the docs scan collapsed to %d file(s) — check _SCANNED_DIRS'
        % len(scanned))

    packs = [p for p in scanned if p.parent.name == 'evidence']
    assert packs, (
        'docs/evidence/ is not being scanned, and it is the directory this '
        'guard exists for')


def test_no_published_doc_carries_an_operator_home_path():
    offenders = []
    for path in _published_markdown():
        text = path.read_text(encoding='utf-8')
        for line_no, line in enumerate(text.splitlines(), start=1):
            for account in _HOME_PATH.findall(line):
                if account.lower() in _ALLOWED_ACCOUNTS:
                    continue
                offenders.append(
                    '%s:%d names the account %r in a home path'
                    % (path.relative_to(REPO_ROOT), line_no, account))

            owner = _LS_OWNER.match(line.strip())
            if owner and owner.group(1).lower() not in _ALLOWED_ACCOUNTS:
                offenders.append(
                    '%s:%d names the account %r in an ls -l owner column'
                    % (path.relative_to(REPO_ROOT), line_no, owner.group(1)))

    assert not offenders, (
        'A published document carries a real account name in an absolute home '
        'path. Replace the account with `<user>`; the path is incidental to '
        'whatever the transcript is evidence for.\n\n' + '\n'.join(offenders))
