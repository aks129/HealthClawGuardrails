"""The run queue and the conversation tables must not depend on the
command center: they are core, and the command center is optional.

MUTATION: put `from r6.command_center.models import ConversationMessage`
(or `from r6 import command_center`, or the relative form) back into any
file under r6/agent_runs/ and test_core_files_do_not_import_the_command_center
fails; an importlib call there fails test_run_queue_imports_with_command_center_blocked.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CORE_FILES = sorted((ROOT / "r6" / "agent_runs").rglob("*.py")) + [
    ROOT / "r6" / "conversations.py"]
FORBIDDEN = "r6.command_center"


def _package_of(path: Path) -> str:
    # A module's package is its directory, for __init__.py and plain files alike.
    return ".".join(path.relative_to(ROOT).parent.parts)


def _imported_modules(source: str, package: str) -> set[str]:
    """Every dotted name an import statement in ``source`` can bind, with
    relative imports resolved against ``package``."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            names.add(module)
            names.update(f"{module}.{a.name}" for a in node.names)
    return names


def _is_forbidden(name: str) -> bool:
    return name == FORBIDDEN or name.startswith(FORBIDDEN + ".")


@pytest.mark.parametrize("source", [
    "from r6.command_center.models import ConversationMessage",
    "import r6.command_center.models",
    "import r6.command_center as cc",
    "from r6 import command_center",
    "from r6 import command_center as cc",
    "from ..command_center.models import ConversationMessage",
    "from .. import command_center",
])
def test_the_import_reader_sees_every_static_shape(source):
    found = _imported_modules(source, "r6.agent_runs")
    assert any(_is_forbidden(n) for n in found), (source, sorted(found))


def test_the_import_reader_does_not_flag_the_core_module():
    found = _imported_modules(
        "from r6.conversations import ConversationMessage\n"
        "from .state import InvalidTransition\n", "r6.agent_runs")
    assert not any(_is_forbidden(n) for n in found), sorted(found)


def test_core_files_do_not_import_the_command_center():
    examined = {str(p.relative_to(ROOT)) for p in CORE_FILES}
    for expected in ("r6/agent_runs/routes.py", "r6/agent_runs/service.py",
                     "r6/agent_runs/models.py", "r6/conversations.py"):
        assert expected in examined, f"not examined: {expected}"
    offenders = {}
    for p in CORE_FILES:
        bad = sorted(n for n in _imported_modules(p.read_text(), _package_of(p))
                     if _is_forbidden(n))
        if bad:
            offenders[str(p.relative_to(ROOT))] = bad
    assert not offenders, offenders


def test_run_queue_imports_with_command_center_blocked():
    """Transitive and dynamic imports: load every run-queue module in a fresh
    interpreter where importing r6.command_center raises."""
    code = (
        "import sys\n"
        "sys.modules['r6.command_center'] = None\n"
        "import r6.conversations, r6.agent_runs, r6.agent_runs.models, "
        "r6.agent_runs.state, r6.agent_runs.service, r6.agent_runs.routes\n"
        "print('ok')\n"
    )
    env = dict(os.environ, HEALTHCLAW_EXTENSIONS="", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stderr[-2000:]


def test_command_center_reexports_the_same_classes():
    from r6 import conversations
    from r6.command_center import models

    for name in ("Conversation", "ConversationMessage", "audit_channel",
                 "KNOWN_CHANNELS", "default_conversation_id"):
        assert getattr(models, name) is getattr(conversations, name)
