"""The run queue and the conversation tables must not depend on the
command center: they are core, and the command center is optional."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_FILES = sorted((ROOT / "r6" / "agent_runs").glob("*.py")) + [
    ROOT / "r6" / "conversations.py"]


def _imported_modules(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_core_files_do_not_import_the_command_center():
    offenders = {
        str(p.relative_to(ROOT)): sorted(
            m for m in _imported_modules(p) if m.startswith("r6.command_center"))
        for p in CORE_FILES
    }
    assert CORE_FILES and not {k: v for k, v in offenders.items() if v}


def test_command_center_reexports_the_same_classes():
    from r6 import conversations
    from r6.command_center import models

    for name in ("Conversation", "ConversationMessage", "audit_channel",
                 "KNOWN_CHANNELS", "default_conversation_id"):
        assert getattr(models, name) is getattr(conversations, name)
