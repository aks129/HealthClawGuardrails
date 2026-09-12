"""Run a dependency audit and say which of two things happened (#589).

The registry not answering is not the same finding as the registry
answering with an advisory, and the dependency-audit job could not tell
them apart: a 503 from the package index failed the job with the same
red, in the same place, as a real vulnerability. This wrapper runs the
audit command, retries a registry outage a few times with backoff, and
if it still cannot reach the registry fails with its own exit code and a
message that names the outage. A finding is never retried and keeps the
tool's own exit code. Nothing here suppresses a failure.

Exit codes:
  0  the audit ran and found nothing
  2  the registry could not be reached after every attempt (an outage)
  otherwise  the tool's own code (a finding, or a tool error), unchanged

Usage:
  python scripts/audit_with_retry.py [--attempts N] [--backoff S] -- <command...>
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

OUTAGE_EXIT = 2

# What a registry outage looks like on stderr/stdout for pip-audit (requests
# under the hood) and npm audit (node's DNS/TCP error names). A real finding
# looks like neither: pip-audit prints the advisory table or JSON; npm prints
# "found N vulnerabilities".
_OUTAGE = re.compile(
    r'503|service unavailable|502 bad gateway|504 gateway|'
    r'connection ?error|connection reset|connection refused|timed? ?out|'
    r'temporary failure in name resolution|name or service not known|'
    r'ENOTFOUND|ECONNRESET|ECONNREFUSED|EAI_AGAIN|ETIMEDOUT|'
    r'failed to (fetch|connect)|could not (fetch|connect)|unable to (fetch|connect)|'
    r'network error|HTTPSConnectionPool|max retries exceeded|'
    r'request to https?://\S+ failed',
    re.IGNORECASE)


# The tools' own verdict lines. Checked BEFORE the outage patterns: an
# advisory title can contain "timeout" or "connection reset", and a real
# finding must never be retried and then reported as an outage.
_FINDING = re.compile(
    r'found \d+ .*vulnerabilit|Found \d+ known vulnerabilit',
    re.IGNORECASE)


def classify(returncode: int, output: str) -> str:
    """'ok', 'finding' or 'outage'.

    A finding is the tool saying so in its summary line; an outage is a
    non-zero exit whose output names the registry not answering; anything
    else non-zero is treated as a finding (never retried, code passed
    through), because a tool error is not an outage either.
    """
    if returncode == 0:
        return 'ok'
    if _FINDING.search(output):
        return 'finding'
    if _OUTAGE.search(output):
        return 'outage'
    return 'finding'


def run_once(command: list[str]) -> tuple[int, str]:
    proc = subprocess.run(command, capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode, output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--attempts', type=int, default=3)
    parser.add_argument('--backoff', type=float, default=10.0,
                        help='seconds before the second attempt; doubles each time')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = [c for c in args.command if c != '--'] if args.command and args.command[0] == '--' else args.command
    if not command:
        parser.error('no audit command given after --')

    delay = args.backoff
    for attempt in range(1, args.attempts + 1):
        code, output = run_once(command)
        verdict = classify(code, output)
        if verdict == 'ok':
            return 0
        if verdict == 'finding':
            # A finding is the tool's answer; it is never retried and its
            # exit code passes through untouched.
            return code
        if attempt < args.attempts:
            print(f'::warning::dependency audit could not reach the registry '
                  f'(attempt {attempt} of {args.attempts}); retrying in {delay:g}s',
                  file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    print('::error::dependency audit could not reach the package registry after '
          f'{args.attempts} attempts. This is a registry outage, NOT a '
          'vulnerability finding; re-run when the registry answers.',
          file=sys.stderr)
    return OUTAGE_EXIT


if __name__ == '__main__':
    sys.exit(main())
