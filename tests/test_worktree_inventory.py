"""Inventory handles Git's NUL framing without treating paths as commands."""

from scripts.worktree_inventory import count_changes, parse_worktrees


def test_paths_with_spaces_and_newlines_and_locked_detached_worktree():
    records = parse_worktrees(
        'worktree /tmp/a space\nline\0HEAD abc\0detached\0'
        'locked held for review\0\0worktree /tmp/b\0HEAD def\0'
        'branch refs/heads/main\0\0'
    )
    assert len(records) == 2
    assert records[0]['worktree'] == '/tmp/a space\nline'
    assert records[0]['detached'] is True
    assert records[0]['locked'] == 'held for review'
    assert records[1]['branch'] == 'refs/heads/main'


def test_renames_do_not_count_source_paths_as_changes():
    assert count_changes('R  new name\0old name\0 M a\nfile\0?? folder/\0') == (2, 1)


def test_clean_output():
    assert count_changes('') == (0, 0)
    assert parse_worktrees('') == []
