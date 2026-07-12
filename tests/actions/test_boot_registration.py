"""Boot registration — importing main ALONE must register every actions
table with SQLAlchemy metadata, or create_all/schema_sync never see it and
new tables/columns silently miss long-lived Postgres (the class of bug
called out in main.py's model-import block, found live 2026-07-08).

Note: an in-process assertion would trivially pass here — pytest imports
every test module at collection time, and tests/actions/test_state_transitions.py
imports r6.actions.events before any test runs, masking a missing boot
import. So the check runs in a clean subprocess that imports ONLY main.
"""
import os
import subprocess
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def test_action_events_table_registered_by_importing_main_alone():
    env = dict(
        os.environ,
        TESTING='1',
        SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
        STEP_UP_SECRET='test-secret-for-hmac-validation',
    )
    code = (
        "import main\n"
        "from models import db\n"
        "assert 'action_events' in db.metadata.tables, "
        "'ActionEvent not registered at boot — add the import to main.py'\n"
    )
    proc = subprocess.run([sys.executable, '-c', code], cwd=_REPO_ROOT,
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
