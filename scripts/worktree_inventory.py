#!/usr/bin/env python3
"""Read-only worktree inventory; does not infer ownership or prune anything."""

import json
import subprocess


def git(*args):
    return subprocess.run(
        ['git', '--no-optional-locks', *args], capture_output=True, check=True
    ).stdout.decode('utf-8', errors='surrogateescape')


def parse_worktrees(raw):
    records = []
    record = {}
    for field in raw.split('\0'):
        if not field:
            if record:
                records.append(record)
                record = {}
            continue
        key, _, value = field.partition(' ')
        record[key] = value if value else True
    if record:
        records.append(record)
    return records


def count_changes(raw):
    # Rename/copy entries consume a second NUL-delimited path.
    entries = iter(raw.split('\0'))
    tracked = untracked = 0
    for entry in entries:
        if not entry:
            continue
        status = entry[:2]
        if status == '??':
            untracked += 1
        else:
            tracked += 1
            if 'R' in status or 'C' in status:
                next(entries, None)
    return tracked, untracked


def inventory():
    records = parse_worktrees(git('worktree', 'list', '--porcelain', '-z'))
    for record in records:
        try:
            raw = git('-C', record['worktree'], 'status', '--porcelain=v1', '-z',
                      '--untracked-files=normal')
            tracked, untracked = count_changes(raw)
            record.update(tracked_changes=tracked, untracked_entries=untracked)
        except subprocess.CalledProcessError:
            record['status_error'] = 'Git status unavailable; inspect manually'
    return records


if __name__ == '__main__':
    print(json.dumps(inventory(), indent=2, ensure_ascii=True))
