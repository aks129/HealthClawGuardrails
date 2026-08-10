"""Drift guards: healthclaw.io templates must track the released version.

The marketing site (templates/, deployed to healthclaw.io on every push) kept
drifting from reality — a v1.1.0 badge and "23 MCP tools"/"712 tests" survived
five releases. These tests make the sync a CI property instead of a memory:
bumping pyproject or the tool manifest without updating the templates fails
the suite (see RELEASING.md step 2).
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _tool_count():
    manifest = json.loads((ROOT / "adapters" / "tools.manifest.json").read_text())
    assert manifest["tool_count"] == len(manifest["tools"])
    return manifest["tool_count"]


def _version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_landing_stats_tool_count_matches_manifest():
    html = (ROOT / "templates" / "index.html").read_text()
    assert f'data-target="{_tool_count()}"' in html, (
        "index.html stats row tool count != adapters manifest tool_count")


def test_no_stale_tool_counts_in_site_copy():
    count = _tool_count()
    for name in ("index.html", "wiki.html"):
        html = (ROOT / "templates" / name).read_text()
        for claimed in re.findall(r"(\d+) MCP tools", html):
            assert int(claimed) == count, (
                f"{name} claims '{claimed} MCP tools' but the manifest has {count}")


def test_base_nav_badge_matches_released_version():
    html = (ROOT / "templates" / "base.html").read_text()
    assert f"v{_version()}" in html, (
        f"base.html nav badge is not v{_version()} — update it with the release "
        "(RELEASING.md step 2)")


def test_health_context_version_matches_release():
    # index.html's nav badge renders v{{ health_context.version }}, sourced
    # from .health-context.yaml — the value that actually shows on
    # healthclaw.io. It sat at 1.3.0 for three releases before this guard.
    m = re.search(r"^version:\s*([\d.]+)", (ROOT / ".health-context.yaml").read_text(),
                  re.MULTILINE)
    assert m and m.group(1) == _version(), (
        f".health-context.yaml version {m.group(1) if m else '(missing)'} != "
        f"pyproject {_version()} — this drives the live site's nav badge")


def test_readme_release_badge_matches_version():
    readme = (ROOT / "README.md").read_text()
    assert f"release-v{_version()}-" in readme, (
        "README release badge is stale vs pyproject version")


# --- numbers the site states about itself ----------------------------------

def _test_function_count() -> int:
    """`def test_` definitions under tests/.

    Deliberately static rather than a pytest collection: collecting from
    inside a run is slow and awkward, and this only needs to be close. The
    collected total is higher than this because of parametrisation, so this
    count is a floor the site's claim must clear.
    """
    total = 0
    for path in (ROOT / "tests").rglob("test_*.py"):
        total += len(re.findall(r"^\s*(?:async\s+)?def test_", 
                                path.read_text(encoding="utf-8", errors="replace"),
                                re.MULTILINE))
    return total


def test_landing_test_count_is_not_stale_and_does_not_overclaim():
    """The headline number on healthclaw.io must be true.

    It was not. The spec strip said 1,665 while the suite had passed 2,780,
    and an audience card on the SAME PAGE said "950+ tests". A reader who
    notices two different totals in one scroll stops believing the other
    numbers, which are the whole argument the page is making.

    The band is deliberately loose. Pinning an exact count would turn every
    added test into a failing build, and a gate that fires on ordinary work
    gets switched off — the failure mode docs/2026-08-02-retro.md describes.
    What this catches is the real one: a number left alone for a year.
    """
    html = (ROOT / "templates" / "index.html").read_text()
    # Pair each number with ITS OWN label. Anchoring on the label alone was not
    # enough: a lazy `.*?` starts at the first data-target on the page (the MCP
    # tool count) and happily runs forward to "Tests passing", so the check
    # compared 29 against 2,289 and failed for the wrong reason. Refusing to
    # cross another data-target is what binds the two together.
    pairs = dict(
        (label.strip(), int(num)) for num, label in re.findall(
            r'data-target="(\d+)"(?:(?!data-target)[\s\S])*?'
            r'class="spec__label">([^<]+)<', html))
    assert pairs, "the landing page no longer states any figures"
    claimed = pairs.get("Tests passing")
    assert claimed, f"no 'Tests passing' figure on the page; found {list(pairs)}"

    floor = _test_function_count()
    assert claimed >= floor, (
        f"healthclaw.io claims {claimed} tests but tests/ defines {floor} test "
        f"functions before parametrisation — the claim understates the suite "
        f"and reads as stale")
    assert claimed <= floor * 2, (
        f"healthclaw.io claims {claimed} tests against {floor} test functions; "
        f"that is more than parametrisation explains")


def test_no_second_test_count_contradicts_the_headline():
    """MUTATION: re-add "950+ tests" to an audience card -> red.

    Two totals on one page is worse than one stale total: it tells the reader
    the page is not maintained, without telling them which number to trust.
    """
    html = (ROOT / "templates" / "index.html").read_text()
    others = re.findall(r"([\d,]+)\+?\s*(?:Python\s+)?tests\b", html, re.I)
    assert not others, (
        f"index.html states a test count in prose ({others}) as well as in the "
        f"stats strip. The strip owns that number; prose citing it drifts.")
