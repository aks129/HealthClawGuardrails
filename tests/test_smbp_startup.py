"""Guard: explicit database initialization must create the SMBP table.

The application factory registers all model metadata but deliberately does no
DDL. The operator-controlled ``initialize_database`` lifecycle hook must still
import SMBPSession before create_all, or POST /r6/smbp/enroll will 500.

So this checks the ENGINE's real table list (what create_all actually built),
not the metadata, using a temp-file SQLite in a clean subprocess.
"""

import os
import subprocess
import sys
import tempfile


def test_smbp_table_physically_created_by_explicit_database_init():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    code = (
        "import main;"
        "main.initialize_database(main.app);"
        "from r6.models import db;"
        "from sqlalchemy import inspect;"
        "ctx = main.app.app_context(); ctx.push();"
        "names = inspect(db.engine).get_table_names();"
        "ctx.pop();"
        "assert 'smbp_sessions' in names,"
        "  'smbp_sessions not created by initialize_database() — add"
        " import r6.smbp.models to register_model_metadata(): ' + repr(names);"
        "print('OK')"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=repo_root,
            env={**os.environ,
                 "SQLALCHEMY_DATABASE_URI": "sqlite:///" + tmp.name},
        )
    finally:
        os.unlink(tmp.name)
    assert result.returncode == 0, (
        "database init failed or smbp_sessions not physically created:\n"
        + result.stdout + result.stderr
    )
    assert "OK" in result.stdout
