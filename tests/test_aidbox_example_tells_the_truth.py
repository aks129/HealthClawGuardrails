"""The published Aidbox example describes the system this repo actually is.

The example ships beside an article, so its failure mode is not a broken
build — it is a reader following instructions that quietly do not work, and
concluding the guardrails do not either. Three such defects shipped in it at
once, and all three passed review, because every one of them is invisible to
a reader and to a linter:

  * `walkthrough.sh` asserted 401 for a bare clinical write. The server
    returns 428: the human-in-the-loop check runs in a before_request hook,
    ahead of the handler's auth gate. The script's first assertion in that
    step would have failed on the reader's first run.
  * The compose file set FHIR_UPSTREAM_CLIENT_ID and _SECRET against an image
    tag that predated the code reading them, so the proxy called Aidbox
    anonymously. Configured correctly; did nothing.
  * The example could not be run here at all — Aidbox needs activating — so
    none of it had been executed end to end when it was written.

The common shape is the catalogue's §0: a confident statement produced by a
check that never ran. The fix that generalises is to make the example's own
claims executable, which is what this file does. It needs no Docker and no
Aidbox: the status codes come from this repo's app, and the example is
checked against them.
"""

import json
import re
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "aidbox-healthclaw-guardrails"
WALKTHROUGH = EXAMPLE / "scripts" / "walkthrough.sh"
COMPOSE = EXAMPLE / "docker-compose.yaml"
README = EXAMPLE / "README.md"

#: A `write <expected> "<label>" [-H 'Header: value' ...]` call in the script.
_WRITE_CALL = re.compile(r"^\s*write\s+(\d{3})\s+\"([^\"]+)\"(.*)$", re.M)
_HEADER_ARG = re.compile(r"-H\s+(['\"])([^'\"]+)\1")


def _write_matrix():
    """(expected_status, label, {header: value}) for each write in step 3."""
    rows = []
    # Join shell line-continuations first. The last row spans two lines, and
    # a line-anchored match silently read it as having one header instead of
    # two — which made this test agree with a matrix that had no fourth row.
    script = re.sub(r"\\\n\s*", " ", WALKTHROUGH.read_text())
    for status, label, rest in _WRITE_CALL.findall(script):
        headers = {}
        for _, raw in _HEADER_ARG.findall(rest):
            name, _, value = raw.partition(":")
            headers[name.strip()] = value.strip()
        rows.append((int(status), label, headers))
    return rows


@pytest.fixture(scope="module")
def matrix():
    rows = _write_matrix()
    # A pin that stops finding its subject reports success forever. Four is
    # the point of the matrix — one row per combination of the two gates.
    assert len(rows) == 4, (
        f"expected 4 write() calls in walkthrough.sh, found {len(rows)}. "
        "If step 3 was restructured, restructure this test with it rather "
        "than relaxing the count.")
    return rows


@pytest.fixture
def no_stray_validator(monkeypatch):
    """Pin the profile validator to unavailable, as the container has it.

    Without this the test fails for exactly the people most likely to run
    it. `FHIR_VALIDATOR_URL` defaults to http://localhost:8080, and
    availability is "GET /health answered under 400" — so an Aidbox running
    on 8080, which is what this very example starts, is mistaken for a
    validator and turns the 201 row into a 422. Issue #488.

    Inside the compose network nothing listens on the proxy's own
    localhost:8080 (Aidbox is at http://aidbox:8080), so unavailable is what
    the example actually runs with. This makes the test agree with that
    rather than with whatever happens to be bound on the developer's laptop.
    """
    from r6.validator import R6Validator
    monkeypatch.setattr(R6Validator, "_is_validator_available",
                        lambda self: False)


class TestTheWalkthroughAssertsWhatTheServerReturns:
    """MUTATION: change any expected status in walkthrough.sh -> red.

    This is the check whose absence let the script ship asserting 401 where
    the server returns 428.
    """

    def test_every_row_matches(self, client, tenant_id, step_up_token, matrix,
                               no_stray_validator):
        body = {
            "resourceType": "Observation",
            "status": "final",
            "subject": {"reference": "Patient/pt-demo"},
            "effectiveDateTime": "2026-08-11",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "85354-9"}]},
            "valueQuantity": {"value": 128, "unit": "mmHg"},
        }
        wrong = []
        for expected, label, headers in matrix:
            sent = {"X-Tenant-Id": tenant_id,
                    "Content-Type": "application/fhir+json"}
            for name, value in headers.items():
                # The script interpolates the token it minted at run time.
                sent[name] = (step_up_token
                              if "${token}" in value or "$token" in value
                              else value)
            response = client.post("/r6/fhir/Observation",
                                   data=json.dumps(body), headers=sent)
            if response.status_code != expected:
                wrong.append(f"{label!r}: script says {expected}, "
                             f"server returns {response.status_code}")
        assert not wrong, (
            "walkthrough.sh asserts status codes this server does not "
            "return. A reader runs this against a fresh checkout and sees "
            "FAIL:\n  " + "\n  ".join(wrong))

    def test_the_two_gates_are_independent(self, matrix):
        """Neither credential nor confirmation alone may reach 2xx.

        The property the matrix exists to demonstrate. A future edit that
        collapsed the matrix back to a two-step sequence would still pass the
        row-by-row check above while losing the thing being shown.
        """
        by_gates = {frozenset(h): status for status, _, h in matrix}
        neither = by_gates[frozenset()]
        both = max(by_gates.items(), key=lambda kv: len(kv[0]))[1]
        singles = [s for gates, s in by_gates.items() if len(gates) == 1]

        assert neither >= 400, "a write with neither gate satisfied was allowed"
        assert len(singles) == 2, "expected one row per gate presented alone"
        assert all(s >= 400 for s in singles), (
            "one gate alone was enough to write. The example claims they do "
            "not substitute for each other.")
        assert both < 400, "both gates satisfied must succeed, or nothing works"


class TestTheReadmeAgreesWithTheScript:
    """The README table is what an article's readers see; most will never run
    the script. It drifting from the script is a published falsehood."""

    def test_the_table_lists_the_same_statuses(self, matrix):
        rows = re.findall(r"^\|\s*(—|`true`)\s*\|\s*(—|valid)\s*\|\s*\*\*(\d{3})\*\*",
                          README.read_text(), re.M)
        assert len(rows) == 4, (
            f"expected the 4-row write matrix in README.md, found {len(rows)}")
        assert sorted(int(r[2]) for r in rows) == sorted(s for s, _, _ in matrix), (
            "the README's status codes and walkthrough.sh's have diverged")


class TestTheComposeFileConfiguresThingsThatExist:
    """MUTATION: add FHIR_UPSTREAM_TOKEN to the compose file -> red.

    The second shipped defect in the list above was a variable the running
    image ignored. This cannot catch a stale IMAGE — only a running container
    can, which is what walkthrough.sh's preflight is for — but it does catch
    the example naming a setting this codebase never reads.
    """

    #: Set by the container runtime or the image, not by our code.
    _NOT_OURS = {"PORT", "PYTHONUNBUFFERED", "TENANT_ID", "FHIR_BASE_URL"}

    def test_every_variable_is_read_somewhere(self):
        import yaml
        compose = yaml.safe_load(COMPOSE.read_text())
        env = compose["services"]["healthclaw"]["environment"]
        names = set(env) - self._NOT_OURS

        root = COMPOSE.resolve().parents[2]
        # Excluding tests is load-bearing, not tidiness. The first version
        # filtered on `"test" not in p.parts`, which excludes a directory
        # named `test` and nothing else — so `tests/` stayed in the haystack,
        # this file's own docstring naming the sentinel variable satisfied the
        # search, and the check passed against a deliberately broken compose
        # file. A guard that reads its own text is measuring itself.
        _SKIP_DIRS = {".venv", "node_modules", ".git", "tests"}
        sources = [p for p in root.rglob("*.py")
                   if not _SKIP_DIRS & set(p.parts)
                   and not p.name.startswith("test_")]
        haystack = "\n".join(p.read_text(errors="ignore") for p in sources)

        unread = sorted(n for n in names if n not in haystack)
        assert not unread, (
            f"the example sets {unread}, which no non-test Python reads. "
            "A setting the app ignores reads to a reviewer exactly like one "
            "it honours.")


class TestTheActivationGateCanActuallyGate:
    """MUTATION: restore `curl -f http://localhost:8080/health` -> red.

    Aidbox answers /health with a 302 to its activation page until it is
    activated, and `curl -f` fails only on 4xx and 5xx. The obvious health
    check therefore reported an unactivated Aidbox as HEALTHY, compose
    started the proxy against a server that serves nothing, and the first
    symptom appeared several steps later somewhere else. Confirmed both ways
    inside the running container: `curl -f` exits 0, an explicit 200 check
    exits 1.

    The catalogue's §12 — a control that cannot work where it is served.
    """

    def test_the_aidbox_check_requires_a_200(self):
        import yaml
        compose = yaml.safe_load(COMPOSE.read_text())
        test = compose["services"]["aidbox"]["healthcheck"]["test"]
        rendered = " ".join(test) if isinstance(test, list) else str(test)
        assert "200" in rendered, (
            "the Aidbox health check does not require a 200. A 302 to the "
            '"Log in to activate Aidbox" page passes `curl -f`, so an '
            "unactivated Aidbox reports healthy and the proxy starts against "
            "a server that answers nothing.")


class TestTheImagePinMatchesThisRepo:
    """MUTATION: bump pyproject's version without the compose file -> red.

    Pinning fixed the stale-`latest` problem and created a new one: a pin can
    name a version that was never published. Tying it to the repo's own
    version means the release that publishes the image is the same commit
    that points the example at it.
    """

    def test_the_pinned_tag_is_this_version(self):
        import tomllib
        root = COMPOSE.resolve().parents[2]
        version = tomllib.loads(
            (root / "pyproject.toml").read_text())["project"]["version"]
        text = COMPOSE.read_text()
        pinned = re.findall(r"image:\s*ghcr\.io/[^:\s]+:(\S+)", text)
        assert pinned, "no ghcr images found in the compose file"
        assert all(tag == version for tag in pinned), (
            f"compose pins {pinned}, repo version is {version}. Publish the "
            "images for this version, or point the example at one that "
            "exists — a pin to an unpublished tag fails at `docker compose "
            "up` with a manifest error.")

    def test_no_image_is_floating(self):
        """`latest` is what went stale in the first place."""
        text = COMPOSE.read_text()
        assert not re.search(r"image:\s*ghcr\.io/\S+:latest", text), (
            "a ghcr image is pinned to :latest. That tag is only "
            "republished when a release is cut, so it silently served a "
            "build that predated the upstream-auth code this example needs.")
