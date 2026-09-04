"""A published document must not cite a path that does not exist.

Twice now a document has asserted a measured claim and named a script as the
backing for it, and the script was not in the repository — #601 and #603.
Both times the claim happened to survive re-running, so nothing broke; both
times it was found by a person stumbling over it. That is the detection
method this file replaces.

#601 added the idea in a form keyed to its own evidence file: "every document
citing *this* evidence still names a path that exists". The property is not
about that evidence file. It is: **anywhere in `docs/`, a path offered as
backing for a claim resolves to something in the tree.** A guard written
against today's filenames does not fire for the document added next week,
which is the one nobody has read.

So the scan is defined by shape, not by a list:

* every `docs/**/*.md`, discovered at run time — a new evidence pack in a new
  subdirectory is covered the moment it lands, with nothing to remember;
* a citation is a token rooted at a real top-level directory of this
  repository (also read at run time) carrying a known source extension;
* a citation fails only when four ways of resolving it all miss.

## What is deliberately NOT scanned, and why

**Design, plan, review, spec and playbook documents.** Their paths are
proposals — `docs/2026-08-02-architecture-audit-and-refactor-plan.md` names
`r6/http/crud.py` as a module the refactor would *create*. Requiring those to
exist would mean a plan could not be written before the code, so this guard
would be deleted rather than obeyed. They are excluded by *kind*
(`_PROPOSAL_DIRS` / `_PROPOSAL_SUFFIX`), never by filename, so a new review
document is excluded on the same rule and a new evidence pack is not.

The cost is a real gap: a spec that says "as `tests/x.py` already pins" is a
claim about the present tense and goes unchecked here. Narrowing that needs
prose classification, not a path check.

**A cited path inside a fenced command block in an excluded document** is also
unchecked. `git add r6/actions/rails/ …` in a plan is a command for a tree
that does not exist yet, so checking fences everywhere trades this guard's
false-negative for false positives in exactly the documents that legitimately
name unbuilt paths.

**Extensionless tokens.** `r6/shc/medent/callback` in
`docs/healthcare-ai-advisors-roadmap.md` is a URL route, and "isolated
skills/personas" is prose. Neither is a file. Requiring an extension is what
separates a citation from a slash in a sentence; the price is that a citation
of a bare *directory* that has been deleted does not fire.

**There is no opt-out marker, and that has a cost.** A note explaining a
citation this guard rejected cannot quote the rejected path — writing the
correction in the set-1 pack turned this file red until the note described the
broken form instead of reproducing it. That is the right trade: a suppression
comment is a hole, and whoever adds the next dead citation would reach for it.
Describe the bad path, do not paste it.

**Category 2 and 3 are out of reach of any path check.** A path that exists
but no longer does what the prose says (`r6/schema_sync.py` was described in
the present tense as dead code after #471 deleted it) resolves fine here. A
measurement with no citation at all — the shape #601 and #603 actually found
— has no path to check. Both were swept by hand in the PR that added this
file; neither is guarded.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / 'docs'

# Document kinds whose paths describe intent rather than the tree.
_PROPOSAL_DIRS = frozenset({'superpowers', 'specs', 'design'})
_PROPOSAL_SUFFIX = re.compile(
    r'(?:-design|-plan|-review|-playbook|-refactor-plan|roadmap)\.md$')

# Extensions that make a slashed token a claim about a file rather than a
# route, a package name, or a sentence containing a slash.
_SOURCE_EXTS = (
    'py', 'sh', 'ts', 'tsx', 'js', 'mjs', 'md', 'json', 'yml', 'yaml',
    'toml', 'html', 'css', 'sql', 'txt', 'cfg', 'ini', 'lock',
)

# Extensions tried when a citation drops the suffix (`r6/access`).
_IMPLIED_EXTS = ('.py', '.sh', '.ts', '.tsx', '.js', '.md', '.yaml', '.yml',
                 '.json', '.html')


def _repo_roots() -> list[str]:
    """Top-level directories, read from the tree rather than hardcoded."""
    return sorted(
        p.name for p in REPO_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith('.') and p.name != 'node_modules'
    )


def _citation_re() -> re.Pattern[str]:
    roots = '|'.join(re.escape(d) for d in _repo_roots())
    exts = '|'.join(_SOURCE_EXTS)
    return re.compile(
        r'(?<![\w/.-])((?:' + roots + r')/[A-Za-z0-9_./-]*'
        r'[A-Za-z0-9_-]\.(?:' + exts + r'))(?![A-Za-z0-9])')


def _is_proposal(doc: Path) -> bool:
    rel = doc.relative_to(REPO_ROOT)
    if _PROPOSAL_DIRS.intersection(rel.parts):
        return True
    return bool(_PROPOSAL_SUFFIX.search(rel.name))


def _scanned_docs() -> list[Path]:
    return [p for p in sorted(DOCS.rglob('*.md')) if not _is_proposal(p)]


def _git_ignores(path: str) -> bool:
    """A citation of a file that is absent *by policy* is not a dead citation.

    `examples/aidbox-healthclaw-guardrails/.env` and `careagents/BUILD_SHA`
    are cited correctly and are supposed to be missing from a clean checkout.
    """
    try:
        return subprocess.run(
            ['git', 'check-ignore', '-q', path],
            cwd=REPO_ROOT, capture_output=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False


def _tree_files() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for p in REPO_ROOT.rglob('*'):
        parts = p.parts
        if '.git' in parts or 'node_modules' in parts or '.venv' in parts:
            continue
        if p.is_file():
            index.setdefault(p.name, []).append(p)
    return index


_TREE = _tree_files()


def resolve_citation(token: str) -> str | None:
    """Return how `token` resolves, or None if nothing in the tree matches.

    Four ways, cheapest first. A citation that is merely *imprecise* — the
    set-1 pack writes `scripts/walkthrough.sh` for a script that lives under
    `examples/` — still names something real, and correcting the prose is the
    sweep's job, not a reason to keep CI red.
    """
    if (REPO_ROOT / token).exists():
        return 'exact'
    if _git_ignores(token):
        return 'gitignored'
    for ext in _IMPLIED_EXTS:
        if (REPO_ROOT / (token + ext)).exists():
            return 'implied-extension'
    tail = token.split('/')[-1]
    matches = [c for c in _TREE.get(tail, ())
               if str(c.relative_to(REPO_ROOT)).endswith(token)]
    if len(matches) == 1:
        return 'unique-suffix'
    return None


def _citations(doc: Path) -> list[tuple[int, str]]:
    pattern = _citation_re()
    found = []
    for line_no, line in enumerate(doc.read_text(encoding='utf-8').splitlines(), 1):
        for match in pattern.finditer(line):
            found.append((line_no, match.group(1)))
    return found


# --------------------------------------------------------------------------
# Guard the guard. A scan that quietly covers nothing passes forever, which is
# how the narrow version of this check would have failed to generalise.
# --------------------------------------------------------------------------

def test_the_scan_reaches_the_directories_it_exists_for():
    scanned = _scanned_docs()
    assert len(scanned) >= 40, (
        'the docs scan collapsed to %d file(s) — check _PROPOSAL_SUFFIX'
        % len(scanned))

    reached = {p.parent.name for p in scanned}
    for required in ('evidence', 'runbooks', 'prd', 'quickstarts'):
        assert required in reached, (
            'docs/%s/ is not being scanned, and it is a directory whose '
            'documents cite scripts as backing for claims' % required)


def test_the_extractor_actually_finds_citations():
    """If the regex stops matching, every other test here passes vacuously."""
    total = sum(len(_citations(doc)) for doc in _scanned_docs())
    assert total >= 100, (
        'only %d path citations found across docs/ — the extractor is broken, '
        'not the documents' % total)


def test_the_extractor_finds_a_citation_it_must_find():
    """A concrete anchor, so a regex that matches only noise is caught."""
    pack = DOCS / 'evidence' / '2026-08-16-set1-guardrail-core.md'
    cited = {token for _, token in _citations(pack)}
    assert any(t.startswith('examples/aidbox-healthclaw-guardrails/')
               for t in cited), (
        'the set-1 evidence pack cites paths under examples/, and the '
        'extractor found none of them')


def test_proposal_documents_are_excluded_by_kind_not_by_name():
    excluded = [p for p in sorted(DOCS.rglob('*.md')) if _is_proposal(p)]
    assert len(excluded) >= 10, 'the proposal exclusion stopped matching'
    # The exclusion must be a rule about kinds, so a document that is neither
    # in a proposal directory nor named like one is never excluded.
    for doc in excluded:
        rel = doc.relative_to(REPO_ROOT)
        assert (_PROPOSAL_DIRS.intersection(rel.parts)
                or _PROPOSAL_SUFFIX.search(rel.name)), rel


def test_resolution_accepts_a_path_that_plainly_exists():
    assert resolve_citation('r6/access.py') == 'exact'


def test_resolution_accepts_a_file_that_is_absent_by_policy():
    assert resolve_citation('careagents/BUILD_SHA') == 'gitignored'


def test_resolution_rejects_a_path_that_is_simply_not_there():
    assert resolve_citation('scripts/this_script_was_never_committed.sh') is None


# --------------------------------------------------------------------------
# The property.
# --------------------------------------------------------------------------

def test_no_published_document_cites_a_path_that_does_not_exist():
    offenders = []
    for doc in _scanned_docs():
        for line_no, token in _citations(doc):
            if resolve_citation(token) is None:
                offenders.append(
                    '%s:%d cites %s, which is not in the tree'
                    % (doc.relative_to(REPO_ROOT), line_no, token))

    assert not offenders, (
        'A published document offers a path as the backing for a claim, and '
        'the path is not in this repository. Either commit the thing it '
        'names, or mark the claim as resting on something uncommitted — an '
        'unverifiable claim labelled as such is useful, a citation that '
        'cannot be followed is not.\n\n' + '\n'.join(offenders))


@pytest.mark.parametrize('doc_name', [
    '2026-08-16-set1-guardrail-core.md',
    '2026-08-16-set2-connectors.md',
])
def test_every_evidence_pack_citation_resolves(doc_name):
    """The evidence packs are the most quoted artifacts this repo produces.

    Stated separately from the sweep above so that a pack failing is reported
    as a pack failing, rather than as one line in a list of every document.
    """
    pack = DOCS / 'evidence' / doc_name
    assert pack.exists(), f'{doc_name} has been renamed or removed'
    unresolved = [
        '%s:%d -> %s' % (pack.name, line_no, token)
        for line_no, token in _citations(pack)
        if resolve_citation(token) is None
    ]
    assert not unresolved, '\n'.join(unresolved)
