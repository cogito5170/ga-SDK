"""OS-level write sandbox for session turns (METHOD R3 by structure, CMD-GA5). Linux, unprivileged user namespaces.

    unshare --user --map-root-user --mount  sh -c '<bind protected paths read-only; bind writable paths read-write>;
                                                   exec unshare --user --mount --map-root-user "$@"'  <command…>

The outer namespace sets the mounts; the command runs one user namespace deeper, where those mounts are
*locked*: it cannot remount them read-write or unmount them to reach what is underneath (the kernel's
MNT_LOCK_* rules for mounts inherited from a more privileged user namespace). So whatever the turn runs —
quoted or abbreviated git options, scripts it wrote, other languages — it cannot write the hub's repositories,
the remotes, the ga directory, or another session's clone. Network is untouched (no network namespace).

What it does not do: hide files (protected paths stay readable), limit CPU or network, or work where
unprivileged user namespaces are off (some distributions) or on macOS / Windows. ``available()`` says.
"""
from __future__ import annotations

import functools
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


@functools.lru_cache(maxsize=1)
def available() -> bool:
    """True when nested unprivileged user + mount namespaces work here."""
    if shutil.which("unshare") is None:
        return False
    probe = ["unshare", "--user", "--map-root-user", "--mount", "sh", "-c",
             "exec unshare --user --mount --map-root-user true"]
    try:
        return subprocess.run(probe, capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _norm(paths: Iterable[str | Path]) -> list[str]:
    out = []
    for p in paths:
        p = Path(p).resolve()
        if p.exists() and str(p) not in out:
            out.append(str(p))
    return out


def script(protect: Iterable[str | Path], writable: Iterable[str | Path]) -> str:
    ro = sorted(_norm(protect), key=lambda s: s.count("/"))
    rw = sorted(_norm(writable), key=lambda s: s.count("/"))
    lines = ["set -e"]
    for p in ro:
        q = shlex.quote(p)
        lines.append(f"mount --bind {q} {q}; mount -o remount,bind,ro {q}")
    for p in rw:
        q = shlex.quote(p)
        lines.append(f"mount --bind {q} {q}; mount -o remount,bind,rw {q}")
    lines.append('exec unshare --user --mount --map-root-user "$@"')
    return "\n".join(lines)


def wrap(argv: list[str], protect: Iterable[str | Path], writable: Iterable[str | Path]) -> list[str]:
    """The command line that runs ``argv`` with ``protect`` read-only and ``writable`` (inside it) read-write."""
    return ["unshare", "--user", "--map-root-user", "--mount", "sh", "-c", script(protect, writable), "ga-sandbox", *argv]
