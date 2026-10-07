"""Bench guard: the module under test must come from this worktree, not from the editable install of the deployed ga-sdk (10-07 15:3x: T2 rev 1 passed with no work because ga.watch resolved to ~/ga-sdk)."""
from pathlib import Path


def test_watch_comes_from_the_worktree():
    import ga.watch as W
    assert Path(W.__file__).resolve().is_relative_to(Path.cwd().resolve()), W.__file__
