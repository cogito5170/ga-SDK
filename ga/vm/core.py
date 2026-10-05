"""The VM installer's logic. Every outside effect goes through a ``Runner`` (argv list, never a shell) and ``disk_free``,
so tests drive it with a temp HOME, local bare remotes and a fake systemctl/loginctl on PATH."""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from ..console import config as K

BRANCH = "claude/gracious-meitner-vp49xe"
BASELINE_URL = "https://github.com/cogito5170/baseline.git"
SDK_URL = "https://github.com/cogito5170/ga-sdk.git"
MIN_FREE_GB = 3.0
PORT = 8765
CONSOLE_UNIT, HUB_UNIT, HUB_TIMER = "ga-console.service", "ga-hub.service", "ga-hub.timer"
UNITS = (CONSOLE_UNIT, HUB_UNIT, HUB_TIMER)
GB = 1024 ** 3
STALE_GLOBS = ("pip-target-*", "pip-unpack-*", "pip-build-*")
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


class VmError(RuntimeError):
    pass


class Runner:
    """Runs one argv (list) and returns (returncode, combined output). A missing program is rc 127."""

    def run(self, argv: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None,
            timeout: float | None = 600) -> tuple[int, str]:
        try:
            p = subprocess.run(list(argv), cwd=cwd, env={**os.environ, **(env or {})}, shell=False, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        except FileNotFoundError:
            return 127, f"{argv[0]}: not found"
        except subprocess.TimeoutExpired:
            return 124, f"{argv[0]}: timed out"
        return p.returncode, p.stdout or ""


def disk_free(path: str | Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:  # a home that is not created yet: its nearest existing parent
        p = p.parent
    return shutil.disk_usage(p).free


def _dir_size(p: Path) -> int:
    n = 0
    for root, _d, files in os.walk(p, followlinks=False):
        for f in files:
            try:
                n += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return n


def stale_pip(tmp: str | Path = "/tmp", top: int = 5) -> list[dict[str, Any]]:
    """The biggest leftover pip temp dirs under ``tmp`` with sizes. Listed only, never deleted."""
    found: list[dict[str, Any]] = []
    t = Path(tmp)
    for pat in STALE_GLOBS:
        try:
            for p in t.glob(pat):
                if p.is_dir() and not p.is_symlink():
                    n = _dir_size(p)
                    found.append({"path": str(p), "bytes": n, "gb": round(n / GB, 3)})
        except OSError:
            pass
    return sorted(found, key=lambda d: -d["bytes"])[:top]


def _os_name() -> str:
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except OSError:
        return platform.system()


def check_disk(home: Path, min_free_gb: float, free: Callable[[Any], int] = disk_free) -> dict[str, Any]:
    gb = free(home) / GB
    return {"home": str(home), "free_gb": round(gb, 2), "min_gb": min_free_gb, "ok": gb >= min_free_gb}


def check(home: Path, *, min_free_gb: float = MIN_FREE_GB, runner: Runner | None = None, free: Callable[[Any], int] = disk_free,
          tmp: str | Path = "/tmp", baseline_url: str = BASELINE_URL, sdk_url: str = SDK_URL,
          user: str | None = None) -> dict[str, Any]:
    r = runner or Runner()
    problems: list[str] = []
    py_ok = sys.version_info >= (3, 10)
    if not py_ok:
        problems.append(f"python {platform.python_version()} < 3.10")
    git_ok = shutil.which("git") is not None
    if not git_ok:
        problems.append("git not found")
    sc_ok = r.run(["systemctl", "--user", "show-environment"], timeout=20)[0] == 0
    if not sc_ok:
        problems.append("systemctl --user is not reachable")
    rc, out = r.run(["loginctl", "show-user", user or getpass.getuser(), "-p", "Linger"], timeout=20)
    linger = rc == 0 and out.strip().endswith("=yes")
    disk = check_disk(home, min_free_gb, free)
    if not disk["ok"]:
        problems.append(f"free disk {disk['free_gb']} GB < {min_free_gb} GB")
    remotes = {}
    for name, url in (("baseline", baseline_url), ("ga-sdk", sdk_url)):
        remotes[name] = git_ok and r.run(["git", "ls-remote", url, "HEAD"], env=GIT_ENV, timeout=30)[0] == 0
        if not remotes[name]:
            problems.append(f"git ls-remote {name} failed")
    return {"ready": not problems, "arch": platform.machine(), "os": _os_name(), "python": platform.python_version(),
            "python_ok": py_ok, "git": git_ok, "systemctl_user": sc_ok, "linger": linger, "disk": disk,
            "stale_pip": stale_pip(tmp), "remotes": remotes, "agy": shutil.which("agy") is not None,
            "problems": problems}


# --- units ---------------------------------------------------------------------------------------------------------

def unit_files(home: Path, *, min_free_gb: float = MIN_FREE_GB) -> dict[str, str]:
    py = f"{home}/ga-venv/bin/python"
    guard = f"{py} -m ga vm check --disk-only --home {home} --min-free-gb {min_free_gb:g}"
    wd = f"{home}/ga-sdk"
    console = (f"[Unit]\nDescription=GA Console (127.0.0.1 only; the one-time URL is in the journal)\n"
               f"After=network-online.target\n\n[Service]\nWorkingDirectory={wd}\nExecStartPre={guard}\n"
               f"ExecStart={py} -m ga console --config {home}/.ga/console.json --port {PORT} --no-open\n"
               f"Restart=always\nRestartSec=5\nNoNewPrivileges=yes\n\n[Install]\nWantedBy=default.target\n")
    hub = (f"[Unit]\nDescription=GA hub tick, shadow mode (a timer runs it every 60 s)\n\n[Service]\nType=oneshot\n"
           f"WorkingDirectory={wd}\nExecStartPre={guard}\nExecStart={py} -m ga hub tick --shadow\nNoNewPrivileges=yes\n")
    timer = ("[Unit]\nDescription=GA hub tick every 60 s\n\n[Timer]\nOnBootSec=60\nOnUnitActiveSec=60\n"
             f"Unit={HUB_UNIT}\n\n[Install]\nWantedBy=timers.target\n")
    return {CONSOLE_UNIT: console, HUB_UNIT: hub, HUB_TIMER: timer}


def has_shadow(home: Path, r: Runner) -> bool:
    rc, out = r.run([f"{home}/ga-venv/bin/python", "-m", "ga", "hub", "tick", "--help"], timeout=60)
    return rc == 0 and "--shadow" in out


def _write(path: Path, text: str, act: Callable[[str], None]) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    act(f"write {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def sdk_pins_hash(sdk: Path) -> str:
    p = sdk / "pyproject.toml"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


# --- git -----------------------------------------------------------------------------------------------------------

def _git(r: Runner, repo: Path, *a: str) -> tuple[int, str]:
    return r.run(["git", "-C", str(repo), *a], env=GIT_ENV, timeout=300)


def plan_repo(r: Runner, repo: Path) -> str:
    """'clone', or 'update' for a clean checkout; VmError for a dirty one or a folder that is not a checkout."""
    if not repo.exists():
        return "clone"
    if _git(r, repo, "rev-parse", "--git-dir")[0] != 0:
        raise VmError(f"{repo} exists but is not a git checkout — not touched")
    rc, out = _git(r, repo, "status", "--porcelain")
    if rc != 0 or out.strip():
        raise VmError(f"{repo} has local changes — commit or stash them, then run again (nothing was changed)")
    return "update"


def sync_repo(r: Runner, repo: Path, url: str, branch: str, how: str, act: Callable[[str], None]) -> None:
    if how == "clone":
        act(f"git clone -b {branch} {url} {repo}")
        rc, out = r.run(["git", "clone", "--branch", branch, url, str(repo)], env=GIT_ENV)
        if rc != 0:
            raise VmError(f"git clone {url} failed: {out.strip()[-200:]}")
        return
    act(f"git fetch origin {branch} + fast-forward {repo}")
    rc, out = _git(r, repo, "fetch", "origin", branch)
    if rc != 0:
        raise VmError(f"git fetch in {repo} failed: {out.strip()[-200:]}")
    cur = _git(r, repo, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()
    if cur != branch:
        rc, out = _git(r, repo, "checkout", branch)
        if rc != 0:
            raise VmError(f"{repo}: cannot switch to {branch}: {out.strip()[-200:]}")
    rc, out = _git(r, repo, "merge", "--ff-only", f"origin/{branch}")
    if rc != 0:
        raise VmError(f"{repo}: {branch} cannot fast-forward (local commits?) — nothing forced: {out.strip()[-200:]}")


# --- install -------------------------------------------------------------------------------------------------------

def install(home: Path, *, dry_run: bool = False, min_free_gb: float = MIN_FREE_GB, runner: Runner | None = None,
            free: Callable[[Any], int] = disk_free, tmp: str | Path = "/tmp", baseline_url: str = BASELINE_URL,
            sdk_url: str = SDK_URL, branch: str = BRANCH, say: Callable[[str], None] = print) -> int:
    r = runner or Runner()

    def act(msg: str) -> None:
        say(("would: " if dry_run else "") + msg)

    verdict = check(home, min_free_gb=min_free_gb, runner=r, free=free, tmp=tmp, baseline_url=baseline_url, sdk_url=sdk_url)
    if not verdict["ready"]:
        say(json.dumps(verdict, ensure_ascii=False))
        say("ga vm install: the check failed (" + "; ".join(verdict["problems"]) + ") — nothing was changed")
        return 1
    base, sdk = home / "baseline", home / "ga-sdk"
    try:
        plans = {base: plan_repo(r, base), sdk: plan_repo(r, sdk)}  # every stop before the first write
    except VmError as e:
        say(f"ga vm install: {e}")
        return 1
    venv, ga_dir = home / "ga-venv", home / ".ga"
    try:
        if dry_run:
            for repo, url in ((base, baseline_url), (sdk, sdk_url)):
                act(f"git {plans[repo]} {repo} ({branch})")
            act(f"python -m venv {venv}; pip install --no-cache-dir -e {sdk} (TMPDIR private, removed on exit)")
        else:
            for repo, url in ((base, baseline_url), (sdk, sdk_url)):
                sync_repo(r, repo, url, branch, plans[repo], act)
            _pip(r, home, venv, sdk, act)
        w = _would if dry_run else _write
        changed = [w(ga_dir / "console.json", json.dumps(K.vm(str(home)), indent=2, ensure_ascii=False) + "\n", act)]
        udir = home / ".config" / "systemd" / "user"
        for name, text in unit_files(home, min_free_gb=min_free_gb).items():
            changed.append(w(udir / name, text, act))
    except VmError as e:
        say(f"ga vm install: {e}")
        return 1
    if dry_run:
        act("systemctl --user daemon-reload; enable --now ga-console.service (and ga-hub.timer when ga has hub tick --shadow)")
    else:
        sc = lambda *a: r.run(["systemctl", "--user", *a], timeout=60)  # noqa: E731
        if any(changed):
            act("systemctl --user daemon-reload")
            sc("daemon-reload")
        if any(changed) or sc("is-enabled", CONSOLE_UNIT)[0] != 0:
            act(f"systemctl --user enable --now {CONSOLE_UNIT}")
            sc("enable", "--now", CONSOLE_UNIT)
            if any(changed):
                sc("restart", CONSOLE_UNIT)
        if has_shadow(home, r):
            if sc("is-enabled", HUB_TIMER)[0] != 0:
                act(f"systemctl --user enable --now {HUB_TIMER}")
                sc("enable", "--now", HUB_TIMER)
        else:
            say("ga-hub: installed but left disabled — this ga has no `ga hub tick --shadow` yet; after an update run `ga vm enable-hub`")
    if not verdict["linger"]:
        say("lingering is off, so the services stop when you log out. Run this yourself (the installer never runs sudo):")
        say("  sudo loginctl enable-linger $USER")
    say(f"ga vm install: done. On the Mac: ssh -N -L {PORT}:127.0.0.1:{PORT} <vm> — the console URL (with its one-time token) "
        f"is in `journalctl --user -u ga-console`")
    return 0


def _would(path: Path, text: str, act: Callable[[str], None]) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    act(f"write {path}")
    return True


def _pip(r: Runner, home: Path, venv: Path, sdk: Path, act: Callable[[str], None]) -> None:
    py = venv / "bin" / "python"
    marker = home / ".ga" / "vm-install.json"
    want = sdk_pins_hash(sdk)
    have = ""
    if marker.is_file():
        try:
            have = json.loads(marker.read_text(encoding="utf-8")).get("pyproject_sha256", "")
        except ValueError:
            pass
    if py.exists() and have == want:
        return
    (home / ".ga").mkdir(parents=True, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="vm-pip-", dir=home / ".ga")
    try:
        env = {"TMPDIR": tmpdir, "PIP_NO_CACHE_DIR": "1"}
        if not py.exists():
            act(f"python -m venv {venv}")
            rc, out = r.run([sys.executable, "-m", "venv", str(venv)], env=env)
            if rc != 0:
                raise VmError(f"venv failed: {out.strip()[-200:]}")
        act(f"pip install --no-cache-dir -e {sdk}")
        rc, out = r.run([str(py), "-m", "pip", "install", "--no-cache-dir", "-e", str(sdk)], env=env, timeout=1800)
        if rc != 0:
            raise VmError(f"pip install failed: {out.strip()[-300:]}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    _write(marker, json.dumps({"pyproject_sha256": want}) + "\n", act)


# --- status, enable-hub, uninstall ---------------------------------------------------------------------------------

def status(home: Path, *, runner: Runner | None = None, free: Callable[[Any], int] = disk_free, say: Callable[[str], None] = print) -> int:
    r = runner or Runner()
    for u in (CONSOLE_UNIT, HUB_TIMER):
        act = r.run(["systemctl", "--user", "is-active", u], timeout=20)[1].strip() or "unknown"
        en = r.run(["systemctl", "--user", "is-enabled", u], timeout=20)[1].strip() or "unknown"
        say(f"{u}: {act} / {en}")
        rc, out = r.run(["journalctl", "--user", "-u", u, "-n", "5", "--no-pager", "-o", "cat"], timeout=20)
        for line in out.strip().splitlines()[-5:]:
            say(f"    {line}")
    say(f"free disk: {free(home) / GB:.1f} GB on {home}")
    say(f"Mac: ssh -N -L {PORT}:127.0.0.1:{PORT} <vm>   then open the URL from `journalctl --user -u ga-console`")
    return 0


def enable_hub(home: Path, *, runner: Runner | None = None, say: Callable[[str], None] = print) -> int:
    r = runner or Runner()
    if not has_shadow(home, r):
        say("ga vm enable-hub: the installed ga has no `ga hub tick --shadow` — update (`ga vm install`) first; left disabled")
        return 1
    rc, out = r.run(["systemctl", "--user", "enable", "--now", HUB_TIMER], timeout=60)
    say(f"ga vm enable-hub: {HUB_TIMER} enabled" if rc == 0 else f"ga vm enable-hub: systemctl failed: {out.strip()}")
    return 0 if rc == 0 else 1


def uninstall(home: Path, *, runner: Runner | None = None, say: Callable[[str], None] = print) -> int:
    r = runner or Runner()
    udir = home / ".config" / "systemd" / "user"
    for u in (HUB_TIMER, HUB_UNIT, CONSOLE_UNIT):
        r.run(["systemctl", "--user", "disable", "--now", u], timeout=60)
    gone = []
    for p in [udir / u for u in UNITS] + [home / ".ga" / "console.json", home / ".ga" / "vm-install.json"]:
        if p.is_file():
            p.unlink()
            gone.append(str(p))
    r.run(["systemctl", "--user", "daemon-reload"], timeout=60)
    say("ga vm uninstall: removed " + (", ".join(gone) if gone else "nothing") + " (checkouts and ~/ga-venv are left)")
    return 0
