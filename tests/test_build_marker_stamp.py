"""The build marker end to end: stamp_build.sh -> BUILD_SHA -> _build._read.

tests/test_careagents.py pins the reader and tests/test_prod_watch_build.py
pins the monitor, but nothing exercised the writer — so the property the whole
design rests on ("exactly one place knows the marker format") was asserted only
in a comment. A stamp_build.sh that emitted one line, or a trailing space, or a
40-char sha would have shipped green: every reader test feeds the reader a
string a human typed, not a string the deploy actually produces.

These run the real script against a throwaway git repo, so clean-vs-dirty and
the refusal paths are exercised for real rather than described.

Three cases below arrived as `xfail(strict=True)` markers for defects found in
review — an `--expect-sha` that parsed away to nothing and passed silently, a
one-character build that prefix-matched every commit, and an unstamped build
claiming it was built in 1970. All three are fixed, so the markers are gone and
the tests now assert the behaviour directly, which is what strict xfail is for.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STAMP = REPO / "deploy" / "careagents" / "stamp_build.sh"

sys.path.insert(0, str(REPO / "scripts"))


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A throwaway repo carrying a copy of the real stamp script.

    Its own tree is controllable, which the checkout running these tests is
    not — the clean-tree case is exactly the one that cannot be tested in
    place, and it is the common case on a release.
    """
    root = tmp_path / "repo"
    (root / "deploy" / "careagents").mkdir(parents=True)
    (root / "careagents").mkdir()
    shutil.copy(STAMP, root / "deploy" / "careagents" / "stamp_build.sh")
    os.chmod(root / "deploy" / "careagents" / "stamp_build.sh", 0o755)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "qa@example.test")
    _git(root, "config", "user.name", "qa")
    (root / "careagents" / "__init__.py").write_text("")
    (root / ".gitignore").write_text("careagents/BUILD_SHA\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _stamp(repo, target, cwd="/"):
    return subprocess.run(
        [str(repo / "deploy" / "careagents" / "stamp_build.sh"), str(target)],
        cwd=cwd, capture_output=True, text=True)


def _read(marker: Path):
    from careagents import _build
    return _build._read(marker)


# --- what the deploy actually writes -----------------------------------------

def test_a_clean_tree_stamps_a_marker_the_reader_accepts(repo):
    # The full contract in one assertion: the writer and the reader agree, and
    # the value is the commit git reports — not a near-miss the reader would
    # silently degrade to "unknown", which is how a stamped deploy would still
    # look unstamped to the monitor.
    r = _stamp(repo, repo)
    assert r.returncode == 0, r.stderr
    sha, when = _read(repo / "careagents" / "BUILD_SHA")
    assert sha == _git(repo, "rev-parse", "--short=12", "HEAD")
    assert when == int(_git(repo, "log", "-1", "--format=%ct"))
    assert len(sha) == 12, "the monitor prefix-matches; a short sha weakens it"


def test_a_clean_tree_is_never_stamped_dirty(repo):
    # `set -e` plus `[ -n "$(git status --porcelain)" ] && SHA=...` would abort
    # the deploy here. The common case must be the safe one.
    r = _stamp(repo, repo)
    assert r.returncode == 0 and "-dirty" not in r.stdout


def test_an_uncommitted_tree_is_stamped_dirty(repo):
    (repo / "careagents" / "scratch.py").write_text("uncommitted\n")
    r = _stamp(repo, repo)
    assert r.returncode == 0
    sha, _ = _read(repo / "careagents" / "BUILD_SHA")
    assert sha.endswith("-dirty")


def test_the_marker_itself_never_makes_the_tree_look_dirty(repo):
    # deploy.sh stamps into the repo root and then rsyncs it. If the marker
    # were not gitignored, the *second* deploy of an otherwise clean tree would
    # stamp itself "-dirty" and the monitor would alarm on a correct release.
    _stamp(repo, repo)
    r = _stamp(repo, repo)
    sha, _ = _read(repo / "careagents" / "BUILD_SHA")
    assert r.returncode == 0 and not sha.endswith("-dirty")


def test_the_repo_gitignores_the_marker():
    # Same guarantee, asserted against the real .gitignore rather than the
    # fixture's: a committed marker goes stale silently (#258).
    r = subprocess.run(["git", "check-ignore", "careagents/BUILD_SHA"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, "careagents/BUILD_SHA must stay gitignored"


def test_a_target_outside_the_repo_gets_an_identical_marker(repo, tmp_path):
    # The Railway path stamps a staging dir that has no .git of its own. If the
    # script resolved the repo from $PWD or from the target, this would stamp
    # the wrong commit or fail — and the Railway deployment is the one that was
    # months stale in #258.
    stage = tmp_path / "stage"
    (stage / "careagents").mkdir(parents=True)
    assert _stamp(repo, stage).returncode == 0
    _stamp(repo, repo)
    assert (stage / "careagents" / "BUILD_SHA").read_bytes() == \
        (repo / "careagents" / "BUILD_SHA").read_bytes()


# --- refusing, loudly, rather than shipping an unmarked build ----------------

@pytest.mark.parametrize("args,code", [
    ([], 2),                       # no argument at all
    ([""], 2),                     # an empty variable, e.g. `"$STAGE"` unset
])
def test_no_target_is_a_usage_error(repo, args, code):
    r = subprocess.run(
        [str(repo / "deploy" / "careagents" / "stamp_build.sh"), *args],
        cwd="/", capture_output=True, text=True)
    assert r.returncode == code and "usage:" in r.stderr


@pytest.mark.parametrize("where", ["missing", "no-careagents"])
def test_a_wrong_target_fails_without_writing_anything(repo, tmp_path, where):
    # Silently writing nothing is the dangerous outcome: the app still boots
    # (marker absent degrades to "unknown") and the deploy check is defeated.
    target = tmp_path / "nope" if where == "missing" else tmp_path / "empty"
    if where == "no-careagents":
        target.mkdir()
    r = _stamp(repo, target)
    assert r.returncode == 1 and "stamp_build" in r.stderr
    if where == "no-careagents":
        assert list(target.iterdir()) == []


def test_an_unwritable_target_leaves_no_partial_marker(repo, tmp_path):
    stage = tmp_path / "ro"
    (stage / "careagents").mkdir(parents=True)
    os.chmod(stage / "careagents", 0o500)
    try:
        r = _stamp(repo, stage)
        assert r.returncode != 0
        assert not (stage / "careagents" / "BUILD_SHA").exists()
    finally:
        os.chmod(stage / "careagents", 0o700)


def test_stamping_from_a_non_repo_fails_instead_of_writing_a_lie(tmp_path):
    # A copy of the script somewhere without a repo above it must not produce
    # a marker at all. Half a marker is worse than none: the reader would
    # accept a sha with no commit behind it.
    fake = tmp_path / "fake"
    (fake / "deploy" / "careagents").mkdir(parents=True)
    (fake / "careagents").mkdir()
    shutil.copy(STAMP, fake / "deploy" / "careagents" / "stamp_build.sh")
    os.chmod(fake / "deploy" / "careagents" / "stamp_build.sh", 0o755)
    r = subprocess.run(
        [str(fake / "deploy" / "careagents" / "stamp_build.sh"), str(fake)],
        cwd="/", capture_output=True, text=True)
    assert r.returncode != 0
    assert not (fake / "careagents" / "BUILD_SHA").exists()


def test_only_stamp_build_knows_the_marker_format():
    # The stated design property. Asserted, because a second `--short=12` added
    # later would drift silently and the two deploy paths would disagree about
    # what a marker means.
    # Shipped code and deploy scripts only. Tests are allowed to name the
    # format — asserting it is what they are for.
    hits = subprocess.run(
        ["git", "grep", "-l", "-e", "short=12", "-e", "format=%ct", "HEAD",
         "--", ":!tests/"],
        cwd=REPO, capture_output=True, text=True).stdout.split()
    assert hits == ["HEAD:deploy/careagents/stamp_build.sh"], hits


# --- the reader, against inputs a human would not type ----------------------

@pytest.mark.parametrize("blob", [
    b"\xff\xfe\x00\x01 not utf-8",                       # binary
    b"\x1b[31m4f2a91cbeef1\x1b[0m\n1754056800\n",        # ANSI escapes
    b"<script>alert(1)</script>\n1\n",                   # markup
    b"4f2a91cbeef1; rm -rf /\n1\n",                      # a sha plus junk
    b"a" * 41 + b"\n1\n",                                # longer than any sha
    b"abcdef\n1\n",                                      # shorter than any sha
    b"-dirty\n1\n",                                      # suffix with no sha
    b"4f2a91cbeef1-dirty-dirty\n1\n",
    b"4f2a91cbeef1\x00\n1\n",                            # embedded NUL
    b"a" * 100_000 + b"\n1\n",                           # enormous single line
])
def test_nothing_but_a_plausible_sha_reaches_the_public_endpoint(tmp_path, blob,
                                                                 monkeypatch):
    # /healthz is unauthenticated. Whatever the marker holds is echoed to the
    # internet verbatim, so the reader is the last gate before that.
    monkeypatch.delenv("CARE_BUILD_SHA", raising=False)
    p = tmp_path / "BUILD_SHA"
    p.write_bytes(blob)
    assert _read(p) == ("unknown", 0)


def test_a_marker_that_is_a_directory_degrades_instead_of_raising(tmp_path):
    # A stray `mkdir careagents/BUILD_SHA` must not take the deployment down:
    # the marker is telemetry, and telemetry must never be an outage.
    assert _read(tmp_path) == ("unknown", 0)


def test_surrounding_whitespace_does_not_defeat_the_marker(tmp_path):
    assert _read_text(tmp_path, "  4f2a91cbeef1  \n  1754056800  \n") == (
        "4f2a91cbeef1", 1754056800)


def test_crlf_line_endings_do_not_defeat_the_marker(tmp_path):
    assert _read_text(tmp_path, "4f2a91cbeef1\r\n1754056800\r\n") == (
        "4f2a91cbeef1", 1754056800)


def _read_text(tmp_path, text):
    p = tmp_path / "BUILD_SHA"
    p.write_text(text, encoding="utf-8")
    return _read(p)


# --- the monitor: open defects ----------------------------------------------

def _prod_watch_with(payload, expect):
    """Run prod_watch against a fully-healthy fake, varying only /healthz."""
    import prod_watch

    class _Resp:
        def __init__(self, status=200, payload=None, text=""):
            self.status_code, self.text, self._p = status, text, payload

        def json(self):
            if self._p is None:
                raise ValueError("no json")
            return self._p

    def get(url, timeout, **kw):
        if "$conformance" in url:
            return _Resp(200, {"grade": "A"})
        if "Condition" in url:
            return _Resp(200, {"entry": [{"resource": {
                "resourceType": "Condition", "code": {"text": "Asthma"}}}]})
        if url.endswith("/healthz"):
            return _Resp(200, payload)
        if url.endswith("/auth"):
            return _Resp(200, text='<input maxlength="8">')
        if url.endswith("/mcp"):
            return _Resp(401)
        return _Resp(200, text='<a href="/auth">start</a>')

    prod_watch.results.clear()
    real, prod_watch.get = prod_watch.get, get
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = prod_watch.run(1.0, expect)
        return code, buf.getvalue()
    finally:
        prod_watch.get = real
        prod_watch.results.clear()


HEALTHY = {"status": "ok", "provider": "openai", "accounts": True}
TIP = "4f2a91cbeef1a9d3c05e7b21fd8460ac9e13d7f5"


def test_a_current_build_is_counted_as_a_real_check():
    code, out = _prod_watch_with({**HEALTHY, "build": "4f2a91cbeef1",
                                  "built_at": 1754056800}, [TIP])
    assert code == 0 and "all 10 checks passing" in out


def test_an_unasserted_build_never_inflates_the_count():
    # The script's honesty property. "all 9 checks passing" must stay literally
    # true when nothing pinned the build.
    code, out = _prod_watch_with({**HEALTHY, "build": "4f2a91cbeef1",
                                  "built_at": 1754056800}, [])
    assert code == 0 and "all 9 checks passing" in out


@pytest.mark.parametrize("supplied", ["", ",", " ", ",,"])
def test_an_expect_sha_that_parses_to_nothing_must_not_pass_silently(supplied,
                                                                    monkeypatch,
                                                                    capsys):
    """`--expect-sha "$SHA"` with SHA unset must not report all-green.

    The original form of this test asserted that the *parse* could never yield
    an empty list, which no implementation can satisfy — "" simply contains no
    sha. What matters is observable: a caller who asked to pin the build must
    never be told everything passed while the pin was silently discarded.
    """
    import prod_watch
    monkeypatch.setattr(sys, "argv",
                        ["prod_watch.py", "--expect-sha", supplied])
    called = []
    monkeypatch.setattr(prod_watch, "run",
                        lambda *a, **k: called.append(a) or 0)
    code = prod_watch.main()
    assert code == 1, "a supplied but empty --expect-sha must fail loudly"
    assert not called, "it must refuse before checking anything"
    assert "all 9 checks passing" not in capsys.readouterr().out


def test_a_one_character_build_must_not_match_every_commit():
    code, _ = _prod_watch_with({**HEALTHY, "build": "4", "built_at": 1}, [TIP])
    assert code == 2, "a build value too short to identify a commit is not proof"


def test_an_unstamped_build_does_not_claim_to_have_been_built_in_1970():
    _, out = _prod_watch_with({**HEALTHY, "build": "unknown", "built_at": 0}, [])
    assert "1970" not in out
