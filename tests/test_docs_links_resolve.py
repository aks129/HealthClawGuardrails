"""Every relative link in a tracked Markdown file points at something.

This is a public repository, and a link into a path that only exists on one
machine is worse than no link: it tells a reader the answer is written down
somewhere they cannot reach. Two of them pointed at `.claude/compliance/`,
which is gitignored and did not exist even locally, from the paragraph telling
a compliance reviewer what to read before touching PHI code.

MUTATION: link to a file that does not exist -> red, naming the file and the
target.
"""
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Links GitHub resolves against the repository URL rather than the file tree.
#: `../../issues/12` works on the site and cannot be checked on disk.
GITHUB_RELATIVE = ("../../issues", "../../pull", "../../discussions",
                   "../../compare", "../../releases", "../../wiki")

LINK = re.compile(r'\[[^\]]*\]\(([^)]+)\)')


def _tracked_markdown():
    listed = subprocess.run(["git", "ls-files", "*.md"], cwd=REPO_ROOT,
                            capture_output=True, text=True, check=True)
    return [REPO_ROOT / name for name in listed.stdout.split()]


def _relative_targets(path):
    for match in LINK.finditer(path.read_text(encoding="utf-8",
                                              errors="replace")):
        target = match.group(1).split("#")[0].split(" ")[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:",
                                            "tel:", "#")):
            continue
        if target.startswith(GITHUB_RELATIVE):
            continue
        yield target


def test_every_relative_markdown_link_resolves():
    broken = []
    for path in _tracked_markdown():
        for target in _relative_targets(path):
            if not (path.parent / target).resolve().exists():
                broken.append("%s -> %s"
                              % (path.relative_to(REPO_ROOT), target))
    assert not broken, (
        "these links point at nothing a reader of this repository can open:\n  "
        + "\n  ".join(sorted(set(broken))))


def test_no_tracked_document_sends_a_reader_into_a_gitignored_path():
    """`.claude/` and `private/` are local. A public document that links into
    them is describing something the reader cannot have.

    MUTATION: link to `.claude/anything.md` from a tracked doc -> red.
    """
    offenders = []
    for path in _tracked_markdown():
        for target in _relative_targets(path):
            resolved = (path.parent / target).resolve()
            try:
                inside = resolved.relative_to(REPO_ROOT)
            except ValueError:
                continue
            if inside.parts and inside.parts[0] in (".claude", "private"):
                offenders.append("%s -> %s"
                                 % (path.relative_to(REPO_ROOT), target))
    assert not offenders, (
        "tracked documents linking into local-only directories: "
        + ", ".join(sorted(set(offenders))))


def test_the_checker_would_notice_a_broken_link(tmp_path):
    """A guard that cannot fail is the defect it was written to catch."""
    doc = tmp_path / "doc.md"
    doc.write_text("[gone](nowhere.md) and [here](doc.md)\n")

    targets = list(_relative_targets(doc))
    missing = [t for t in targets if not (doc.parent / t).exists()]

    assert targets == ["nowhere.md", "doc.md"]
    assert missing == ["nowhere.md"]
