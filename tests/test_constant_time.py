"""A credential comparison is total: no input spelling can crash a gate.

`hmac.compare_digest` raises TypeError on a `str` that is not ASCII, and
`str.encode` raises UnicodeEncodeError on a lone surrogate. Every gate in
this repository feeds it a value the caller controls, so one non-ASCII byte
in a header, a query argument or a token turned an authorization refusal into
an unauthenticated 500 (#557).

`r6/constant_time.py` makes the comparison total instead of screening the
input, so a credential that cannot be spelled is refused as what it is — a
credential that does not match — through the same door as any other wrong
one. See the module docstring for why that is the choice rather than a
pre-check.

MUTATION: revert `equal` to `hmac.compare_digest(provided, expected)` on the
raw strings -> every case in TestEqualIsTotal reddens.
MUTATION: change `as_bytes` to `value.encode('utf-8')` ->
test_a_lone_surrogate_compares_without_raising reddens.
"""

import ast
import pathlib

import pytest

from r6 import constant_time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The same production tree tests/test_ratchets.py walks, for the same
#: reason: a scan that walks nothing passes forever.
_PRODUCTION_DIRS = ('r6', 'careagents', 'adapters', 'api', 'services',
                    'scripts', 'openclaw', 'hermes', 'migrations')
_PRODUCTION_ROOT_FILES = ('main.py', 'app.py', 'models.py')
_SCAN_FLOOR = 100


def _production_python_files():
    for name in _PRODUCTION_ROOT_FILES:
        path = REPO_ROOT / name
        if path.exists():
            yield path
    for folder in _PRODUCTION_DIRS:
        base = REPO_ROOT / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' not in path.parts:
                yield path


# ---------------------------------------------------------------------------
# The comparison is total
# ---------------------------------------------------------------------------

class TestEqualIsTotal:

    @pytest.mark.parametrize('provided', [
        'plain-ascii-but-wrong',
        'café',            # a latin-1 name, what a header actually carries
        '€' * 8,           # outside latin-1 entirely
        '\U0001f600',           # astral plane
        '\ud800',               # a lone surrogate: json.loads can produce one
        '',
        b'raw-bytes',
    ], ids=['ascii', 'latin1', 'euro', 'astral', 'surrogate', 'empty',
            'bytes'])
    def test_a_wrong_credential_answers_false_however_it_is_spelled(
            self, provided):
        """No spelling of a wrong value raises; every one of them is False."""
        assert constant_time.equal(provided, 'the-expected-secret') is False

    def test_a_lone_surrogate_compares_without_raising(self):
        """`'\\ud800'.encode('utf-8')` raises. 'surrogatepass' is what makes
        `as_bytes` total, so this case is what keeps it in the source."""
        assert constant_time.equal('\ud800', '\ud800') is True

    def test_equal_values_are_equal(self):
        assert constant_time.equal('s3cret', 's3cret') is True
        assert constant_time.equal(b's3cret', 's3cret') is True
        assert constant_time.equal('café', 'café') is True

    def test_the_expected_half_may_also_be_unspellable(self):
        """A misconfigured secret is a server problem, not a crash lever: the
        environment decodes with surrogateescape, so `expected` can carry the
        same shapes `provided` can."""
        assert constant_time.equal('x', '\udcff') is False

    def test_bytes_pass_through_unchanged(self):
        assert constant_time.as_bytes(b'\xff\xfe') == b'\xff\xfe'
        assert constant_time.as_bytes(bytearray(b'ab')) == b'ab'


# ---------------------------------------------------------------------------
# One home
# ---------------------------------------------------------------------------

#: `hmac.compare_digest` is allowed in exactly these production modules.
#: Each entry is a decision with a reason, not an exemption granted by
#: whoever wrote the line.
_COMPARE_DIGEST_HOMES = {
    'r6/constant_time.py':
        'the one home: it owns the encode so no caller has to remember it',
    'careagents/accounts.py':
        'both operands are hexdigests this process just computed, so no '
        'caller-supplied string reaches the comparison. CareAgents cannot '
        'import r6 either (tests/test_import_acyclicity.py), so routing it '
        'through the helper would mean duplicating the helper',
}


def test_compare_digest_has_one_home():
    """MUTATION: call `hmac.compare_digest` directly in a new gate -> red.

    Not a ratchet (those are counts that only fall); an invariant. r6/ held 13
    copies of this comparison and 11 of them passed a caller-supplied `str`
    straight in, because a convention repeated at N call sites is the
    generator behind `docs/2026-08-02-retro.md`. A new gate reaching for
    `hmac.compare_digest` gets the non-total version by default, so the
    default has to be unavailable rather than discouraged.
    """
    offenders = []
    scanned = 0
    for path in _production_python_files():
        scanned += 1
        rel = str(path.relative_to(REPO_ROOT))
        if rel in _COMPARE_DIGEST_HOMES:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and node.attr == 'compare_digest'):
                offenders.append(f'{rel}:{node.lineno}')
    assert scanned >= _SCAN_FLOOR, (
        f'walked only {scanned} production modules; the path list is broken '
        'and this test is reporting a green it did not measure')
    assert not offenders, (
        f'{offenders} call hmac.compare_digest directly. It raises TypeError '
        'on a non-ASCII str (#557), so a caller-supplied value must reach it '
        'through r6.constant_time.equal, which owns the encode. If the '
        'operands genuinely cannot come from a caller, add the module to '
        '_COMPARE_DIGEST_HOMES with the reason.')


def test_every_allowed_home_still_exists_and_records_why():
    """A home listed for a file that no longer uses the call is dead
    configuration, and dead configuration reads as coverage."""
    for rel, why in _COMPARE_DIGEST_HOMES.items():
        path = REPO_ROOT / rel
        assert path.exists(), f'{rel} is allowlisted but does not exist'
        assert 'compare_digest' in path.read_text(encoding='utf-8'), (
            f'{rel} is allowlisted but no longer calls compare_digest')
        assert len(why) > 30, f'{rel} is allowlisted without a recorded reason'
