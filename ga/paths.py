"""Real paths (CMD-GA41 S5). On macOS /var is a link to /private/var (and /tmp to /private/tmp): a path stored one way and
compared with a path written the other way differ by text, though they are the same directory. Wherever ga stores or
compares a worktree / ga-dir / repository path it uses ``real`` on both sides. Standard library only."""
from __future__ import annotations

import os
from pathlib import Path


def real(p: str | os.PathLike) -> str:
    """The path with every symlink resolved, absolute (os.path.realpath; a missing tail is kept as written)."""
    return os.path.realpath(os.fspath(p))


def same(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    return real(a) == real(b)


def real_path(p: str | os.PathLike) -> Path:
    return Path(real(p))


__all__ = ["real", "same", "real_path"]
