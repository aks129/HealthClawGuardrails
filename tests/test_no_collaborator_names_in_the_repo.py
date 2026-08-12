"""This repository is public. Collaborators' names do not belong in it.

Fifteen tracked files named one clinical advisor — in production code, in
tests, and in design docs — because every defect she reported got written up
with attribution. The attribution was meant as credit and reads, in a public
repository, as a disclosure: it links a named physician to a product, to
specific clinical opinions, and in one case to the prefix of a tenant id
provisioned for her.

None of it was needed. What a comment has to carry is WHO REPORTED IT well
enough to explain why the code is shaped that way — "a clinical reviewer",
"a design partner", "a licensed clinician" — and the substance survives the
name coming out.

WHAT THIS GUARD DOES AND DOES NOT DO

It fails when a known collaborator name appears in a tracked file. It is a
ratchet against recurrence, not a remedy: these names are still in git
history, and history is not rewritten here because the repository is public
and already cloned. Removing them going forward is the part that is actually
available.

The list lives here rather than in a config file so that adding a name is a
code review. If you are adding one, the question to answer in the PR is why
the person needs naming at all.
"""

import pathlib
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Collaborators who have appeared in this repo. Surnames and handles only —
#: a first name alone is too collision-prone to scan for ("ray" is a demo
#: persona, "sally" is a bot).
_NAMES = (
    "magan",
    "yimdriuska",
)

#: This file necessarily contains the names it forbids.
_SELF = "tests/test_no_collaborator_names_in_the_repo.py"

#: Tenants known to hold a real person's records. A curated list, not a
#: shape: `<slug>-<hex>` also matches synthetic fixtures
#: (`sharp-0123456789abcdef`), Railway project uuids and content hashes, and
#: a guard that flags four fixtures per true positive gets deleted rather
#: than obeyed. Add to this list when a tenant is provisioned for a person.
_REAL_TENANT_PREFIXES = ("gene-1ff1ecf2", "gigi-")


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True)
    for rel in out.stdout.splitlines():
        if rel == _SELF:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf",
                                   ".woff2", ".ico", ".mp4", ".webm", ".zip"}:
            continue
        try:
            yield rel, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def test_no_collaborator_name_appears_in_a_tracked_file():
    """MUTATION: put a collaborator's surname back in any comment -> red."""
    hits = []
    for rel, text in _tracked_text_files():
        lowered = text.lower()
        for name in _NAMES:
            if name in lowered:
                hits.append(f"{rel}: {name}")
    assert not hits, (
        "a collaborator is named in a public repository:\n  "
        + "\n  ".join(hits)
        + "\nDescribe the ROLE instead — 'a clinical reviewer', 'a design "
          "partner'. The reason the code is shaped that way survives the "
          "name coming out.")


def test_no_real_tenant_id_appears_in_a_tracked_file():
    """A provisioned tenant id is a pointer to one person's records.

    Two of these were committed in design docs describing a live shakeout —
    the procedure was the lesson and the id was an input, so the docs now
    carry `$OWNER_TENANT` and the value lives in the environment.

    MUTATION: put the literal tenant back in either doc -> red.
    """
    hits = []
    for rel, text in _tracked_text_files():
        lowered = text.lower()
        for prefix in _REAL_TENANT_PREFIXES:
            if prefix in lowered:
                hits.append(f"{rel}: {prefix}")
    assert not hits, (
        "a tenant provisioned for a real person is committed:\n  "
        + "\n  ".join(hits)
        + "\nUse a placeholder and keep the value in the environment.")
