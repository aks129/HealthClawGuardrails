"""The three scripts behind §5, §6 and §7 of the set-2 pack, pinned.

Issue #602: sections 5, 6 and 7 of `docs/evidence/2026-08-16-set2-connectors.md`
rested on scripts that lived only in an uncommitted scratch directory. The
directory is gone, so nobody could re-run the highest-severity finding of that
pass (R5) or the half-configured-upstream finding (R1). #601 settled §3 and §4
the same way; this is the rest.

What this file guards, and what it deliberately does not:

  * The scripts and their transcripts stay committed, and the documents that
    cite them keep naming paths that exist. A document pointing at a deleted
    script is the defect this whole thread is about.

  * The scripts never print an absolute path. An operator's home-directory
    name in an evidence pack merged to a public repository is the 2026-08-16
    incident; the 2026-08-16 pack had to redact one. Fixed in the scripts
    (`short()`), and this is what stops it coming back. Asserted by RUNNING
    them, not by reading them for a string.

  * The 2026-08-16 numbers the scripts compare against still match the pack.
    Both scripts carry a hard-coded baseline so the comparison survives the
    pack being edited; that is only safe while the two agree, and this is
    where a disagreement surfaces.

  * Each script refuses rather than reporting a pass when it cannot measure.
    `smoke_medplum.py` scoring 7/8 against a Medplum that did not exist (pack
    entry R2) is the failure mode.

It does NOT re-pin what the connector registry does. `hapi` being AUTH_BASIC
(#512), the misconfigured/degraded health state (#513) and refusing to boot on
an unknown kind (#518) are already pinned by
`tests/test_upstream_connector_registry.py` and `tests/test_fhir_proxy.py`. A
second copy of a pin is a second thing to keep in step.

Needs no Docker, no network and no FHIR server: every subprocess here is
given input it must refuse before it reaches one.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "docs" / "evidence" / "2026-08-16-set2-connectors.md"
RERUN = ROOT / "docs" / "evidence" / "2026-09-04-set2-sections-5-7-rerun.md"
TRANSCRIPTS = ROOT / "docs" / "evidence" / "2026-09-04-set2-rerun"

REGISTRY_CONTRACT = ROOT / "scripts" / "connector-registry-contract.py"
AUTH_PROBE = ROOT / "scripts" / "connector-auth-probe.py"
IMAGE_PINS = ROOT / "scripts" / "image-pin-digests.sh"

PYTHON_SCRIPTS = (REGISTRY_CONTRACT, AUTH_PROBE)

TRANSCRIPT_NAMES = (
    "registry-contract-run.txt",
    "registry-contract-at-2b7872d-run.txt",
    "mutation-registry-contract.txt",
    "auth-probe-run.txt",
    "auth-probe-at-2b7872d-run.txt",
    "negative-control-auth-probe.txt",
    "mutation-auth-probe.txt",
    "image-pins-run.txt",
    "negative-control-image-pins.txt",
    "mutation-tests.txt",
)


def _run(argv, cwd=ROOT):
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, timeout=120
    )


class TestTheEvidenceStaysCheckableByAStranger:
    """#602's own property. A cited path that does not exist is the defect."""

    @pytest.mark.parametrize(
        "path", [REGISTRY_CONTRACT, AUTH_PROBE, IMAGE_PINS, RERUN, PACK]
    )
    def test_the_scripts_and_documents_are_committed(self, path):
        assert path.exists(), (
            f"{path.relative_to(ROOT)} is gone. §5, §6 and §7 of the set-2 "
            "pack rest on it."
        )

    @pytest.mark.parametrize("name", TRANSCRIPT_NAMES)
    def test_the_transcripts_are_committed(self, name):
        assert (TRANSCRIPTS / name).exists(), (
            f"{name} is gone. It is the raw output the re-run's findings are "
            "read from; without it the findings are assertions again."
        )

    def test_the_rerun_document_names_only_paths_that_exist(self):
        """Every repo path the write-up cites in backticks must resolve."""
        cited = set()
        for match in re.findall(r"`([^`\n]+)`", RERUN.read_text()):
            candidate = re.sub(r":\d+$", "", match.strip()).rstrip(".")
            if candidate.startswith(("scripts/", "docs/", "tests/", "r6/")):
                cited.add(candidate)
        assert cited, "the write-up cites no repository paths at all"
        missing = sorted(p for p in cited if not (ROOT / p).exists())
        assert not missing, f"the re-run write-up cites paths that do not exist: {missing}"

    def test_the_correction_to_the_packs_stale_citation_is_itself_correct(self):
        """The write-up says `is_proxy_enabled` moved. Check both halves.

        MUTATION: point the write-up at a different line number -> red.
        """
        proxy = (ROOT / "r6" / "fhir_proxy.py").read_text().splitlines()
        lines = [
            n for n, text in enumerate(proxy, 1)
            if text.startswith("def is_proxy_enabled(")
        ]
        assert len(lines) == 1, "is_proxy_enabled is not defined once in r6/fhir_proxy.py"
        assert "def is_proxy_enabled(" not in (ROOT / "r6" / "routes.py").read_text(), (
            "is_proxy_enabled is back in r6/routes.py; the write-up's correction "
            "to the pack's citation is now wrong."
        )
        assert f"r6/fhir_proxy.py:{lines[0]}" in RERUN.read_text(), (
            f"is_proxy_enabled is at r6/fhir_proxy.py:{lines[0]}, which is not "
            "the line the write-up cites. A citation that has drifted is the "
            "defect this whole thread is about."
        )


class TestNoOperatorUsernameReachesAnEvidencePack:
    """The 2026-08-16 incident: a home-directory path in a public repo.

    Asserted by running the scripts, not by grepping them for `short(`. The
    string could survive a change that stops it being used.
    """

    @pytest.mark.parametrize("name", TRANSCRIPT_NAMES)
    def test_the_committed_transcripts_carry_no_home_path(self, name):
        text = (TRANSCRIPTS / name).read_text()
        assert "/Users/" not in text and "/home/" not in text, (
            f"{name} contains a home-directory path. An operator's OS "
            "username in an evidence pack merged to a public repo is the "
            "2026-08-16 incident."
        )

    @pytest.mark.parametrize("script", PYTHON_SCRIPTS, ids=lambda p: p.name)
    def test_a_script_never_prints_an_absolute_checkout_path(self, script, tmp_path):
        """The checkout it was pointed at comes back relative, always.

        Absolute is the property under test, not "contains /Users/": the
        temporary directory pytest hands this test is itself under a path
        carrying the operator's name, and a relative path to it is correct
        output from the script even so.

        MUTATION: print `{repo}` instead of `{short(repo)}` -> red.
        """
        result = _run([sys.executable, str(script), "--repo", str(tmp_path)])
        output = result.stdout + result.stderr
        printed = re.search(r"no main\.py under (\S+)", output)
        assert printed, (
            f"{script.name} did not report the checkout it was given: {output[:200]!r}"
        )
        assert not printed.group(1).startswith("/"), (
            f"{script.name} printed the absolute path {printed.group(1)!r}. "
            "An absolute path here is the operator's home-directory name."
        )


class TestTheBaselinesStillMatchThePack:
    """The scripts compare today against 2026-08-16 from a hard-coded copy.

    Hard-coded on purpose — a baseline that follows the document it is checked
    against cannot detect anything. The cost is that the two can drift apart
    silently, which is what these tests are for.
    """

    def test_every_image_digest_the_script_compares_against_is_in_the_pack(self):
        """MUTATION: change one hex digit in the script's baseline -> red."""
        script = IMAGE_PINS.read_text()
        digests = set(re.findall(r"BASELINE_\w+='(sha256:[0-9a-f]{64})'", script))
        assert len(digests) == 4, (
            f"expected §7's four baseline digests in {IMAGE_PINS.name}, "
            f"found {len(digests)}"
        )
        pack = PACK.read_text()
        missing = sorted(d for d in digests if d not in pack)
        assert not missing, (
            "image-pin-digests.sh compares against digests that §7 of the "
            f"pack does not record: {missing}. One of the two moved."
        )

    def test_the_auth_probe_compares_against_what_section_6_recorded(self):
        """MUTATION: flip the script's baseline to {'hapi': 'Basic'} -> red."""
        script = AUTH_PROBE.read_text()
        baseline = re.search(r"^BASELINE = (\{.*\})$", script, re.M)
        assert baseline, "the auth probe no longer declares a 2026-08-16 baseline"
        assert baseline.group(1) == '{"hapi": None, "generic": "Basic"}', (
            "the auth probe's baseline changed. §6 recorded "
            "`kind=hapi -> Authorization: None` and `kind=generic -> "
            "Authorization: Basic`; a baseline that does not say that is "
            "comparing against something other than the pack."
        )
        section6 = PACK.read_text().split("## 6.", 1)[-1].split("## 7.", 1)[0]
        assert "kind=hapi     -> Authorization: None" in section6
        assert "kind=generic  -> Authorization: Basic" in section6


class TestAScriptRefusesRatherThanReportingAPass:
    """R2's failure mode: 7 of 8 green against a server that did not exist."""

    @pytest.mark.parametrize("script", PYTHON_SCRIPTS, ids=lambda p: p.name)
    def test_a_checkout_with_no_app_is_refused(self, script, tmp_path):
        result = _run([sys.executable, str(script), "--repo", str(tmp_path)])
        assert result.returncode != 0, (
            f"{script.name} exited 0 against a directory with no app in it. "
            "A run that booted nothing must not read as a run that passed."
        )
        assert "PASS" not in result.stdout, (
            f"{script.name} reported a PASS having booted nothing"
        )

    def test_a_compose_file_with_no_images_is_refused(self, tmp_path):
        compose = tmp_path / "compose.yaml"
        compose.write_text('services:\n  x:\n    ports:\n      - "1:1"\n')
        result = _run(["bash", str(IMAGE_PINS), str(compose)])
        assert result.returncode != 0, (
            "image-pin-digests.sh exited 0 against a compose file naming no "
            "images. §7's question went unanswered and the script said so."
        )
        assert "PASS" not in result.stdout

    def test_a_missing_compose_file_is_refused(self, tmp_path):
        result = _run(["bash", str(IMAGE_PINS), str(tmp_path / "nope.yaml")])
        assert result.returncode != 0
        assert "PASS" not in result.stdout
