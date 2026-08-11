"""The conformance CLI does not write into a tenant by accident (#463).

The probes create synthetic Patients and Observations in whatever tenant they
grade, and never remove them. The CLI's documented example read
`--tenant desktop-demo` — the tenant the product demos from — and `--tenant`
was required, so following the example was the path of least resistance.

Six probe patients ("Zzyzxbarton, Quintavious") accumulated in production
desktop-demo that way. They were the second source behind a physician
advisor's report of duplicate records on camera, the day before a launch
recording; the first was a non-idempotent seed (#457).

The self-conformance ENDPOINT never had this problem. r6/conformance/routes.py
has always written to a dedicated tenant "so a caller's data is never touched."
The two paths disagreed about a rule one of them had already established, and
the one with a documented example pointing at the demo tenant was the one
people ran by hand.

MUTATION: restore `required=True` on --tenant, or point the default back at
desktop-demo -> the tests here redden.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "guardrail_conformance.py"


def _source():
    return CLI.read_text(encoding="utf-8")


def test_the_cli_and_the_endpoint_share_one_selftest_tenant():
    """MUTATION: hardcode the tenant string in the CLI -> red.

    Two copies of a tenant name drift, and the drift is invisible until one
    of them is pointed somewhere real.
    """
    from r6.conformance.snapshot import SELFTEST_TENANT
    src = _source()
    assert "from r6.conformance.snapshot import SELFTEST_TENANT" in src, (
        "the CLI no longer shares the endpoint's selftest tenant constant")
    assert f'"{SELFTEST_TENANT}"' not in src, (
        "the CLI hardcodes the selftest tenant instead of importing it")


def test_tenant_defaults_to_the_selftest_tenant():
    """MUTATION: `required=True` on --tenant -> red."""
    from r6.conformance.snapshot import SELFTEST_TENANT
    src = _source()
    block = re.search(r'ap\.add_argument\("--tenant".*?\)\n', src, re.S)
    assert block, "--tenant argument not found"
    arg = block.group(0)
    assert "default=SELFTEST_TENANT" in arg, (
        "--tenant no longer defaults to the dedicated selftest tenant")
    assert "required=True" not in arg, (
        "--tenant is required again, so the documented example is once more "
        "the thing people copy")
    assert SELFTEST_TENANT == "conformance-selftest"


def test_no_documented_example_points_at_a_demo_tenant():
    """MUTATION: put `--tenant desktop-demo` back in the docstring -> red.

    Checks the whole file, not just the usage block: a demo tenant in a
    --help string or a comment gets copied just as readily.
    """
    src = _source()
    for tenant in ("desktop-demo", "gigi-", "demo-tenant"):
        assert tenant not in src, (
            f"{tenant!r} appears in the conformance CLI. The probes write "
            "and do not clean up, so any tenant named here is one somebody "
            "will point them at (#463).")


def test_the_help_text_says_the_probes_write():
    """A flag whose help text does not mention the writes is how this
    happened. 'a public/synthetic tenant is fine' was the old wording; it is
    true about the DATA and silent about the side effect."""
    src = _source()
    block = re.search(r'ap\.add_argument\("--tenant".*?\)\n', src, re.S)
    assert block
    help_text = block.group(0).lower()
    assert "write" in help_text, (
        "--help does not tell the operator the probes write into this tenant")
    assert "clean up" in help_text or "do not remove" in help_text, (
        "--help does not tell the operator the probes leave the data behind")


def test_overriding_the_default_warns_before_it_writes():
    """MUTATION: delete the warning branch -> red."""
    src = _source()
    assert "if args.tenant != SELFTEST_TENANT:" in src, (
        "the CLI no longer warns when asked to grade a different tenant")
    warn = src.split("if args.tenant != SELFTEST_TENANT:", 1)[1][:600]
    assert "stderr" in warn, "the warning does not go to stderr"
    assert "will not remove" in warn or "not remove" in warn, (
        "the warning does not say the data stays behind")


def test_the_docstring_no_longer_claims_a_run_is_harmless():
    """The sentence that made this defect readable as safe.

    "a live run never touches real patient records" was true about the
    synthetic data the probes send, and read by everyone as a statement about
    the tenant. A reassurance that is technically true and practically
    misleading is docs/defect-catalogue.md §1.
    """
    src = _source()
    assert "never touches real patient records" not in src, (
        "the docstring again claims a live run is harmless; it writes into "
        "the tenant it grades")
    assert "The probes WRITE" in src, (
        "the docstring no longer leads with the fact that the probes write")
