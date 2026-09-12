"""The dependency audit tells a registry outage from a finding (#589)."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'audit_with_retry.py'
sys.path.insert(0, str(ROOT / 'scripts'))
from audit_with_retry import OUTAGE_EXIT, classify  # noqa: E402


@pytest.mark.parametrize('code, output, verdict', [
    (0, 'No known vulnerabilities found', 'ok'),
    (1, 'Found 2 known vulnerabilities in 1 package', 'finding'),
    (1, 'found 3 vulnerabilities (1 moderate, 2 high)', 'finding'),
    (1, 'HTTPError: 503 Server Error: Service Unavailable for url: https://pypi.org/pypi/x/json', 'outage'),
    (1, 'npm ERR! code ECONNRESET\nnpm ERR! network request to https://registry.npmjs.org/-/npm/v1/security/advisories/bulk failed', 'outage'),
    (2, 'ConnectionError: HTTPSConnectionPool(host=\'pypi.org\', port=443): Max retries exceeded', 'outage'),
    (1, 'getaddrinfo EAI_AGAIN registry.npmjs.org', 'outage'),
    (3, 'some other tool error', 'finding'),
    # A finding whose advisory title happens to name an outage word is
    # still a finding: the summary line wins.
    (1, 'Regular Expression Denial of Service via timeout in some-lib\n'
        'found 1 high severity vulnerability', 'finding'),
    (1, 'Found 1 known vulnerability in 1 package\n'
        'Name Version ID Fix Versions\n'
        'requests 2.0 GHSA-xxxx connection reset handling', 'finding'),
])
def test_classify(code, output, verdict):
    assert classify(code, output) == verdict


def _fake_audit(tmp_path, script_body):
    fake = tmp_path / 'fake_audit.sh'
    fake.write_text('#!/bin/sh\n' + script_body)
    fake.chmod(0o755)
    return str(fake)


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def test_a_transient_outage_is_retried_and_then_passes(tmp_path):
    counter = tmp_path / 'n'
    counter.write_text('0')
    fake = _fake_audit(tmp_path, f'''
n=$(cat {counter}); n=$((n+1)); echo $n > {counter}
if [ $n -lt 3 ]; then echo "HTTPError: 503 Service Unavailable" >&2; exit 1; fi
echo "No known vulnerabilities found"; exit 0
''')
    r = _run('--attempts', '3', '--backoff', '0', '--', fake)
    assert r.returncode == 0, r.stderr
    assert counter.read_text().strip() == '3'
    assert r.stderr.count('::warning::') == 2


def test_a_sustained_outage_fails_with_its_own_code_and_message(tmp_path):
    fake = _fake_audit(tmp_path, 'echo "npm ERR! code ENOTFOUND" >&2; exit 1\n')
    r = _run('--attempts', '3', '--backoff', '0', '--', fake)
    assert r.returncode == OUTAGE_EXIT
    assert '::error::' in r.stderr
    assert 'registry outage, NOT a vulnerability finding' in r.stderr


def test_a_finding_is_never_retried_and_keeps_the_tools_exit_code(tmp_path):
    counter = tmp_path / 'n'
    counter.write_text('0')
    fake = _fake_audit(tmp_path, f'''
n=$(cat {counter}); n=$((n+1)); echo $n > {counter}
echo "Found 1 known vulnerability in 1 package"; exit 1
''')
    r = _run('--attempts', '3', '--backoff', '0', '--', fake)
    assert r.returncode == 1
    assert counter.read_text().strip() == '1'
    assert '::error::' not in r.stderr and '::warning::' not in r.stderr


def test_ci_routes_both_audits_through_the_wrapper():
    """MUTATION: call pip-audit or npm audit directly in ci.yml -> red."""
    text = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text()
    job = text.split('  dependency-audit:', 1)[1].split('  compliance-gates:', 1)[0]
    lines = [ln for ln in job.splitlines() if 'pip-audit --strict' in ln or 'npm audit --audit-level=high' in ln]
    assert len(lines) == 2, lines
    assert all('scripts/audit_with_retry.py' in ln for ln in lines), lines
    assert '|| true' not in job
