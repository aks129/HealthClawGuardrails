"""The run queue and the conversation tables must not depend on the
command center: they are core, and the command center is optional.

MUTATION: put `from r6.command_center.models import ConversationMessage`
(or `from r6 import command_center`, or the relative form) back into any
file under r6/agent_runs/ and test_core_files_do_not_import_the_command_center
fails. So does `importlib.import_module("r6.command_center.models")` or
`__import__(...)` with a literal name, wherever it sits (a function body, a
try/except). A module-level import reached transitively fails
test_run_queue_imports_with_command_center_blocked.

Not covered: a dynamic import whose module name is computed at runtime.
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


def _dynamic_import_names(source: str) -> set[str]:
    """Literal module names passed to importlib.import_module or __import__,
    anywhere in ``source``, including inside functions and try blocks."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        called = (func.attr if isinstance(func, ast.Attribute)
                  else func.id if isinstance(func, ast.Name) else None)
        arg = node.args[0]
        if called in ("import_module", "__import__") and isinstance(
                arg, ast.Constant) and isinstance(arg.value, str):
            names.add(arg.value)
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


@pytest.mark.parametrize("source", [
    "import importlib\nimportlib.import_module('r6.command_center.models')",
    "from importlib import import_module\n"
    "def f():\n    return import_module('r6.command_center')",
    "def f():\n    try:\n        __import__('r6.command_center.routes')\n"
    "    except ImportError:\n        pass",
])
def test_the_dynamic_reader_sees_literal_calls_anywhere(source):
    found = _dynamic_import_names(source)
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
        source = p.read_text()
        bad = sorted(n for n in (_imported_modules(source, _package_of(p))
                                 | _dynamic_import_names(source))
                     if _is_forbidden(n))
        if bad:
            offenders[str(p.relative_to(ROOT))] = bad
    assert not offenders, offenders


def test_run_queue_imports_with_command_center_blocked():
    """Module-level transitive imports: load every run-queue module in a fresh
    interpreter where importing r6.command_center raises, then check that no
    command-center module was loaded."""
    code = (
        "import sys\n"
        "sys.modules['r6.command_center'] = None\n"
        "import r6.conversations, r6.agent_runs, r6.agent_runs.models, "
        "r6.agent_runs.state, r6.agent_runs.service, r6.agent_runs.routes\n"
        "print(sorted(m for m, v in sys.modules.items()\n"
        "             if v is not None and m.startswith('r6.command_center')))\n"
    )
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().splitlines()[-1] == "[]", r.stdout[-2000:]


def test_command_center_reexports_the_same_classes():
    from r6 import conversations
    from r6.command_center import models

    for name in ("Conversation", "ConversationMessage", "audit_channel",
                 "KNOWN_CHANNELS", "default_conversation_id"):
        assert getattr(models, name) is getattr(conversations, name)


def test_the_moved_tables_keep_their_names_and_constraints():
    """No migration rides on this move, so the schema identity is pinned."""
    from r6.conversations import Conversation, ConversationMessage

    assert Conversation.__table__.name == "cc_conversations"
    assert ConversationMessage.__table__.name == "cc_conversation_messages"
    conv = {c.name for c in Conversation.__table__.constraints}
    msg = {c.name for c in ConversationMessage.__table__.constraints}
    assert "pk_cc_conversations" in conv
    assert {"fk_cc_message_conversation", "uq_cc_message_request"} <= msg
    assert [c.name for c in Conversation.__table__.primary_key.columns] == [
        "tenant_id", "id"]


def test_the_moved_tables_keep_their_column_types():
    """Widths matter on Postgres; this pins them without a database."""
    from r6.conversations import Conversation, ConversationMessage

    def cols(model):
        return {c.name: str(c.type) for c in model.__table__.columns}

    assert cols(Conversation) == {
        "id": "VARCHAR(128)", "tenant_id": "VARCHAR(64)",
        "agent_id": "VARCHAR(64)", "created_by_surface": "VARCHAR(32)",
        "status": "VARCHAR(24)", "created_at": "DATETIME",
        "updated_at": "DATETIME"}
    assert cols(ConversationMessage) == {
        "id": "VARCHAR(64)", "tenant_id": "VARCHAR(64)",
        "conversation_id": "VARCHAR(128)", "agent_id": "VARCHAR(64)",
        "channel": "VARCHAR(32)", "session_id": "VARCHAR(128)",
        "user_id": "VARCHAR(128)", "role": "VARCHAR(16)", "text": "TEXT",
        "request_id": "VARCHAR(128)", "reply_to": "VARCHAR(64)",
        "metadata_json": "TEXT", "created_at": "DATETIME"}


def test_the_conversation_tables_are_registered_by_name():
    """Registered directly, not only as a side effect of the command
    center's re-export, so removing that shim cannot drop the tables."""
    import bundled_extensions
    from r6.extensions import ExtensionRegistry

    registry = ExtensionRegistry()
    bundled_extensions.register(registry)
    assert "r6.conversations" in registry.model_modules
