"""The VM installer's logic. Every outside effect goes through a ``Runner`` (argv list, never a shell) and ``disk_free``,
so tests drive it with a temp HOME, local bare remotes and a fake systemctl/loginctl on PATH."""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import re
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
TOKEN_URL = "https://github.com/cogito5170/Token.git"
MIN_FREE_GB = 3.0
PORT = 8765
CONSOLE_UNIT, HUB_UNIT, HUB_TIMER = "ga-console.service", "ga-hub.service", "ga-hub.timer"
UNITS = (CONSOLE_UNIT, HUB_UNIT, HUB_TIMER)
BRIDGE_UNIT = "ga-bridge.service"  # --full only
UPDATE_UNIT, UPDATE_TIMER = "ga-update.service", "ga-update.timer"  # --full only (CMD-GA48)
NOTICE_TO, NOTICE_FROM = "baseline-ops", "vm"
AGENTS = ("minimal", "ga-plan", "ga-act", "ga-ask")  # the Mac's three + ga-ask, the ask default since GA44
BRIDGE_NAME = "AGY"
ADOPTED = "bridge-adopted"  # ~/.ga/bridge-adopted: written by `ga vm bridge-adopt`, required by the bridge unit
TWO_BRIDGES = "turn the Mac bridge off first — two bridges answer the same directive twice"
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
    agy = shutil.which("agy") is not None
    agy_version = None
    if agy:
        rc, out = r.run(["agy", "--version"], timeout=30)
        agy_version = (out.strip().splitlines() or [""])[0][:80] if rc == 0 else None
    return {"ready": not problems, "arch": platform.machine(), "os": _os_name(), "python": platform.python_version(),
            "python_ok": py_ok, "git": git_ok, "systemctl_user": sc_ok, "linger": linger, "disk": disk,
            "stale_pip": stale_pip(tmp), "remotes": remotes, "agy": agy, "agy_version": agy_version,
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
           f"WorkingDirectory={wd}\nExecStartPre={guard}\nExecStart={py} -m ga hub tick --shadow --config {home}/.ga/hub.json "
           f"--ga-dir {home}/.ga\nNoNewPrivileges=yes\n")
    timer = ("[Unit]\nDescription=GA hub tick every 60 s\n\n[Timer]\nOnBootSec=60\nOnUnitActiveSec=60\n"
             f"Unit={HUB_UNIT}\n\n[Install]\nWantedBy=timers.target\n")
    return {CONSOLE_UNIT: console, HUB_UNIT: hub, HUB_TIMER: timer}


def update_units(home: Path) -> dict[str, str]:
    """CMD-GA48 S2: the self-update oneshot and its timer (2 min after boot, then every 30 min)."""
    py = f"{home}/ga-venv/bin/python"
    svc = (f"[Unit]\nDescription=GA VM self-update (fast-forward the checkouts, reinstall only on change)\n\n[Service]\n"
           f"Type=oneshot\nWorkingDirectory={home}\nExecStart={py} -m ga vm update --home {home}\nNoNewPrivileges=yes\n")
    timer = ("[Unit]\nDescription=GA VM self-update every 30 min\n\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=30min\n"
             f"Unit={UPDATE_UNIT}\n\n[Install]\nWantedBy=timers.target\n")
    return {UPDATE_UNIT: svc, UPDATE_TIMER: timer}


def hub_conf(home: Path, *, branch: str = BRANCH) -> dict[str, Any]:
    """~/.ga/hub.json for the VM's shadow hub (CMD-GA45 S1): absolute paths only (Mailbox does not expand ~)."""
    h = Path(home)
    return {"name": "baseline", "human": "human", "mailbox_repo": str(h / "baseline"), "baseline_repo": str(h / "baseline"),
            "directives_dir": str(h / "baseline" / "directives"),
            "repos": {"cogito5170/ga-sdk": {"path": str(h / "ga-sdk"), "base": branch},
                      "cogito5170/Token": {"path": str(h / "token"), "base": branch}},
            "backend": "agv", "model": "auto", "options": {"agent": "ga-plan"}, "shadow": True,
            "daily_turns": 40}


GA_HUB_MODELS = ("gemini-3.1-pro-high", "gpt-oss-120b-medium")  # the hub.json models ga itself ever wrote (CMD-GA51 S2)


def migrate_hub_model(home: Path, *, dry_run: bool = False, say: Callable[[str], None] = print) -> bool:
    """CMD-GA51 S2: ~/.ga/hub.json "model" -> "auto" only when it still holds a value ga wrote; a model the user wrote
    stays. One line when it changes. True when it was (or would be) rewritten."""
    f = Path(home) / ".ga" / "hub.json"
    try:
        conf = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    old = conf.get("model") if isinstance(conf, dict) else None
    if old not in GA_HUB_MODELS:
        return False
    if dry_run:
        say(f"would: {f} model {old} -> auto (follow the bridge's served model)")
        return True
    conf["model"] = "auto"
    f.write_text(json.dumps(conf, indent=2) + "\n", encoding="utf-8")
    say(f"ga vm: {f} model {old} -> auto (follow the bridge's served model)")
    from .. import events as EV
    EV.emit("SYSTEM", "vm-update", "hub model auto", "DONE", old=old)
    return True


def bridge_unit(home: Path, *, min_free_gb: float = MIN_FREE_GB) -> str:
    """The agy bridge as a user unit (CMD-OPS2 S3). Installed by --full, enabled only by `ga vm enable-bridge --yes`;
    ExecStartPre stops it with one clear line when agy, the adopt marker or ~/agy-bridge.json is missing."""
    py = f"{home}/ga-venv/bin/python"
    guard = f"{py} -m ga vm check --disk-only --home {home} --min-free-gb {min_free_gb:g}"
    path = f"{home}/.local/bin:{home}/bin:/usr/local/bin:/usr/bin:/bin"
    return (f"[Unit]\nDescription=agy bridge for to/{BRIDGE_NAME} (enable with `ga vm enable-bridge --yes` after the Mac "
            f"bridge is off)\nAfter=network-online.target\n\n[Service]\nWorkingDirectory={home}/baseline\n"
            f"Environment=PATH={path}\nExecStartPre={guard}\nExecStartPre={py} -m ga vm bridge-ready --home {home}\n"
            f"ExecStart={py} {home}/baseline/ops/agy_bridge/bridge.py --config {home}/agy-bridge.json\n"
            f"Restart=always\nRestartSec=30\nNoNewPrivileges=yes\n\n[Install]\nWantedBy=default.target\n")


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
    rc, top = _git(r, repo, "rev-parse", "--show-toplevel")  # a plain folder inside a git home would answer for the parent
    if rc != 0 or Path(top.strip()).resolve() != repo.resolve():
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
            sdk_url: str = SDK_URL, branch: str = BRANCH, say: Callable[[str], None] = print, full: bool = False,
            token_url: str = TOKEN_URL) -> int:
    r = runner or Runner()

    def act(msg: str) -> None:
        say(("would: " if dry_run else "") + msg)

    verdict = check(home, min_free_gb=min_free_gb, runner=r, free=free, tmp=tmp, baseline_url=baseline_url, sdk_url=sdk_url)
    if not verdict["ready"]:
        say(json.dumps(verdict, ensure_ascii=False))
        say("ga vm install: the check failed (" + "; ".join(verdict["problems"]) + ") — nothing was changed")
        return 1
    base, sdk, token = home / "baseline", home / "ga-sdk", home / "token"
    repos = [(base, baseline_url), (sdk, sdk_url)] + ([(token, token_url)] if full else [])
    try:
        plans = {repo: plan_repo(r, repo) for repo, _u in repos}  # every stop before the first write
    except VmError as e:
        say(f"ga vm install: {e}")
        return 1
    venv, ga_dir = home / "ga-venv", home / ".ga"
    agy = bool(verdict.get("agy"))
    try:
        if dry_run:
            for repo, url in repos:
                act(f"git {plans[repo]} {repo} ({branch})")
            act(f"python -m venv {venv}; pip install --no-cache-dir -e {sdk} (TMPDIR private, removed on exit)")
            if full:
                act(f"python -m venv {token}/.venv; pip install --no-cache-dir -e {token}/backend; npm ci in "
                    f"{token}/frontend (TMPDIR and npm cache private; disk guard before and after)")
        else:
            for repo, url in repos:
                sync_repo(r, repo, url, branch, plans[repo], act)
            _pip(r, home, venv, sdk, act)
            if full:
                _token(r, home, act, lambda stage: _guard(home, min_free_gb, free, stage))
        w = _would if dry_run else _write
        missing = _prereqs(r, home) if full else {}
        cfg = K.vm_full(str(home), _disabled(missing)) if full else K.vm(str(home))
        changed = [w(ga_dir / "console.json", json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", act)]
        udir = home / ".config" / "systemd" / "user"
        units = unit_files(home, min_free_gb=min_free_gb)
        if full:
            units[BRIDGE_UNIT] = bridge_unit(home, min_free_gb=min_free_gb)
            units.update(update_units(home))
        for name, text in units.items():
            changed.append(w(udir / name, text, act))
        if full:
            hub_json = ga_dir / "hub.json"
            if not hub_json.exists():  # never the user's own file (CMD-GA45 S1)
                w(hub_json, json.dumps(hub_conf(home, branch=branch), indent=2) + "\n", act)
            else:
                migrate_hub_model(home, dry_run=dry_run, say=say)
            ask = home / ".ga-ask" / "ask.json"
            if not ask.exists():  # never the user's own file
                w(ask, json.dumps({"ask_agent": "ga-ask"}, indent=2) + "\n", act)
            _agents(r, home, agy, dry_run, act, say)
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
    if full and not dry_run and sc("is-enabled", UPDATE_TIMER)[0] != 0:
        act(f"systemctl --user enable --now {UPDATE_TIMER}")
        sc("enable", "--now", UPDATE_TIMER)
    if full:
        _say_missing(home, missing, say)
        say(f"{BRIDGE_UNIT}: installed, not enabled — see `ga vm bridge-adopt` and `ga vm enable-bridge` (docs/VM.md)")
    if not verdict["linger"]:
        say("lingering is off, so the services stop when you log out. Run this yourself (the installer never runs sudo):")
        say("  sudo loginctl enable-linger $USER")
    say(f"ga vm install: done. On the Mac: ssh -N -L {PORT}:127.0.0.1:{PORT} <vm> — the console URL (with its one-time token) "
        f"is in `journalctl --user -u ga-console`")
    return 0


def _guard(home: Path, min_free_gb: float, free: Callable[[Any], int], stage: str) -> None:
    gb = free(home) / GB
    if gb < min_free_gb:
        raise VmError(f"disk guard {stage}: {gb:.1f} GB free < {min_free_gb:g} GB — stopped; nothing new was enabled")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def _state(home: Path) -> dict[str, Any]:
    f = home / ".ga" / "vm-full.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except ValueError:
        return {}


def _save_state(home: Path, st: dict[str, Any], act: Callable[[str], None]) -> None:
    _write(home / ".ga" / "vm-full.json", json.dumps(st, sort_keys=True) + "\n", act)


def _token(r: Runner, home: Path, act: Callable[[str], None], guard: Callable[[str], None]) -> None:
    """~/token/.venv with the backend installed and ~/token/frontend's npm ci, each skipped when its pins are unchanged.
    Private TMPDIR (and npm cache) removed on exit; the disk guard runs before each step and after npm ci."""
    token, st = home / "token", _state(home)
    venv_py = token / ".venv" / "bin" / "python"
    want_be, want_fe = _sha(token / "backend" / "pyproject.toml"), _sha(token / "frontend" / "package-lock.json")
    tmpdir = tempfile.mkdtemp(prefix="vm-full-", dir=home / ".ga")
    try:
        env = {"TMPDIR": tmpdir, "PIP_NO_CACHE_DIR": "1", "npm_config_cache": os.path.join(tmpdir, "npm-cache")}
        if not (venv_py.exists() and st.get("backend_sha256") == want_be):
            guard("before the Token backend install")
            if not venv_py.exists():
                act(f"python -m venv {token}/.venv")
                rc, out = r.run([sys.executable, "-m", "venv", str(token / ".venv")], env=env)
                if rc != 0:
                    raise VmError(f"Token venv failed: {out.strip()[-200:]}")
            act(f"pip install --no-cache-dir -e {token}/backend")
            rc, out = r.run([str(venv_py), "-m", "pip", "install", "--no-cache-dir", "-e", str(token / "backend")],
                            env=env, timeout=1800)
            if rc != 0:
                raise VmError(f"Token backend pip install failed: {out.strip()[-300:]}")
            st["backend_sha256"] = want_be
            _save_state(home, st, act)
        if not ((token / "frontend" / "node_modules").is_dir() and st.get("frontend_sha256") == want_fe):
            guard("before npm ci")
            act(f"npm ci in {token}/frontend")
            rc, out = r.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=str(token / "frontend"), env=env, timeout=1800)
            if rc != 0:
                raise VmError(f"npm ci failed: {out.strip()[-300:]}")
            guard("after npm ci")
            st["frontend_sha256"] = want_fe
            _save_state(home, st, act)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _prereqs(r: Runner, home: Path) -> dict[str, bool]:
    """What the user must set up (never the installer): True = missing."""
    pg = (r.run(["pg_isready", "-q", "-h", "127.0.0.1", "-p", "5432"], timeout=20)[0] == 0
          and r.run(["psql", "-d", "gaconsole", "-Atqc", "select 1"], timeout=20)[0] == 0)
    return {"postgresql": not pg, "env": not (home / "token" / ".env").is_file(),
            "bridge_config": not (home / "agy-bridge.json").is_file()}


def _disabled(missing: dict[str, bool]) -> dict[str, str]:
    out = {"bridge": f"runs as {BRIDGE_UNIT} (ga vm enable-bridge), not from the console"}
    why = [w for k, w in (("postgresql", "PostgreSQL / the gaconsole database"), ("env", "~/token/.env")) if missing.get(k)]
    if why:
        for n in ("token-api", "token-worker", "token-web"):
            out[n] = "missing: " + ", ".join(why) + " (see `ga vm install --full` output)"
    return out


def env_names(example: Path) -> list[str]:
    """The key NAMES of a .env.example (never its values)."""
    names = []
    try:
        lines = example.read_text(encoding="utf-8").splitlines()
    except OSError:
        return names
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, sep, _v = line.partition("=")
        if sep and not line.startswith("#") and k.strip().isidentifier():
            names.append(k.strip())
    return names


def _say_missing(home: Path, missing: dict[str, bool], say: Callable[[str], None]) -> None:
    t = home / "token"
    if missing.get("postgresql"):
        say("PostgreSQL 16 (127.0.0.1:5432) with the gaconsole database is missing. Run this yourself (the installer never runs sudo):")
        for c in ("sudo apt-get install -y postgresql", "sudo -u postgres createuser $USER",
                  "sudo -u postgres createdb -O $USER gaconsole",
                  f"psql gaconsole -v ON_ERROR_STOP=1 -qf {t}/docs/schema.sql"):
            say(f"  {c}")
    if missing.get("env"):
        say(f"{t}/.env is missing. Make it yourself (dev-only values made on this machine; never printed, never committed):")
        say(f"  cd {t} && python3 scripts/dev_env.py --db-host localhost")
        say(f"  sed -i 's|^DATABASE_URL=.*|DATABASE_URL=postgresql:///gaconsole|; s|^GC_UPLOAD_DIR=.*|GC_UPLOAD_DIR={home}/.ga/gc-uploads|' .env")
        say("  (postgresql:///gaconsole is the local socket: Ubuntu's peer login, no password)")
        names = env_names(t / ".env.example")
        if names:
            say("  names it must hold (from .env.example): " + ", ".join(names))
    if missing.get("postgresql") or missing.get("env"):
        say("token-api, token-worker, token-web: left disabled in console.json — run `ga vm install --full` again afterwards")
    if missing.get("bridge_config"):
        say(f"{home}/agy-bridge.json is missing. Copy the Mac's and change its /Users/<you> paths to {home}:")
        say(f"  scp <mac>:~/agy-bridge.json {home}/agy-bridge.json")
        ex = sorted((home / "baseline" / "ops" / "agy_bridge").glob("*example*.json"))
        if ex:
            try:
                keys = list(json.loads(ex[0].read_text(encoding="utf-8")))
                say(f"  fields (from {ex[0].name}): " + ", ".join(map(str, keys)))
            except (OSError, ValueError, TypeError):
                pass


def _agents(r: Runner, home: Path, agy: bool, dry_run: bool, act: Callable[[str], None], say: Callable[[str], None]) -> None:
    py = str(home / "ga-venv" / "bin" / "python")
    argvs = {n: [py, "-m", "ga", "agy-agent", "install", "--name", n] for n in AGENTS}
    if not agy:
        say("agy is not installed: install it and log in yourself, then run (or `ga vm install --full` again):")
        for a in argvs.values():
            say("  " + " ".join(a))
        return
    st = _state(home)
    done = set(st.get("agents") or [])
    for n, a in argvs.items():
        if n in done:
            continue
        act(" ".join(a))
        if dry_run:
            continue
        rc, out = r.run(a, timeout=180)
        if rc != 0:
            say(f"agy-agent {n}: failed ({out.strip()[-200:]}); run it yourself: " + " ".join(a))
            continue
        done.add(n)
        st["agents"] = sorted(done)
        _save_state(home, st, act)


def _would(path: Path, text: str, act: Callable[[str], None]) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    act(f"write {path}")
    return True


def _pip(r: Runner, home: Path, venv: Path, sdk: Path, act: Callable[[str], None], by_head: bool = False) -> bool:
    """True when pip ran. ``by_head`` (the self-update): a new ga-sdk HEAD reinstalls too, not only new pins."""
    py = venv / "bin" / "python"
    marker = home / ".ga" / "vm-install.json"
    want = sdk_pins_hash(sdk)
    head = _git(r, sdk, "rev-parse", "HEAD")[1].strip()
    have, have_head = "", ""
    if marker.is_file():
        try:
            m = json.loads(marker.read_text(encoding="utf-8"))
            have, have_head = m.get("pyproject_sha256", ""), m.get("head", "")
        except ValueError:
            pass
    if py.exists() and have == want and (not by_head or have_head == head):
        return False
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
    _write(marker, json.dumps({"pyproject_sha256": want, "head": head}) + "\n", act)
    return True


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
    say("last update: " + (last_update(home) or "none yet (ga-update.timer is installed by `ga vm install --full`)"))
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
    for u in (BRIDGE_UNIT, HUB_TIMER, HUB_UNIT, CONSOLE_UNIT):
        r.run(["systemctl", "--user", "disable", "--now", u], timeout=60)
    gone = []
    for p in [udir / u for u in UNITS + (BRIDGE_UNIT,)] + [home / ".ga" / n for n in ("console.json", "vm-install.json", "vm-full.json", ADOPTED)]:
        if p.is_file():
            p.unlink()
            gone.append(str(p))
    r.run(["systemctl", "--user", "daemon-reload"], timeout=60)
    say("ga vm uninstall: removed " + (", ".join(gone) if gone else "nothing") + " (checkouts and ~/ga-venv are left)")
    return 0


# --- the bridge handoff and remote access (CMD-OPS2) ----------------------------------------------------------------

def bridge_adopt(home: Path, *, name: str = BRIDGE_NAME, say: Callable[[str], None] = print) -> int:
    """Mark every message now in to/<name> read in the VM's baseline clone (the bridge reads ``Mailbox.unread(name)``),
    so a fresh bridge does not re-run the directives the Mac bridge already answered; then write the adopt marker."""
    from ..mailbox import NAME, MailError, Mailbox
    if not NAME.match(name or ""):
        say(f"ga vm bridge-adopt: {name!r} is not a mailbox name")
        return 2
    box = Mailbox(home / "baseline")
    try:
        tip = box.tip()
        paths = box._files(tip, f"to/{name}/")
        seen = box.read_set(name)
    except MailError as e:
        say(f"ga vm bridge-adopt: {e} — nothing marked")
        return 1
    new = [p for p in paths if p not in seen]
    for p in new:
        box.mark_read(name, p)
    marker = home / ".ga" / ADOPTED
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"name": name, "tip": tip, "marked": len(new), "messages": len(paths)}) + "\n",
                      encoding="utf-8")
    say(f"ga vm bridge-adopt: marked {len(new)} message(s) in to/{name} read ({len(paths)} in all); wrote {marker}")
    return 0


def bridge_ready(home: Path, *, say: Callable[[str], None] = print) -> int:
    """The bridge unit's ExecStartPre: one clear line and exit 1 when something it needs is missing."""
    why = []
    if shutil.which("agy") is None:
        why.append("agy is not on PATH (install it and log in)")
    if not (home / ".ga" / ADOPTED).is_file():
        why.append("the mailbox is not adopted (run `ga vm bridge-adopt` after the Mac bridge is off)")
    if not (home / "agy-bridge.json").is_file():
        why.append(f"{home}/agy-bridge.json is missing")
    if why:
        say("ga-bridge not started: " + "; ".join(why))
        return 1
    return 0


def enable_bridge(home: Path, *, yes: bool = False, runner: Runner | None = None, say: Callable[[str], None] = print) -> int:
    say(TWO_BRIDGES)
    if not yes:
        say("ga vm enable-bridge: give --yes once the Mac bridge is off — nothing was enabled")
        return 1
    if not (home / ".config" / "systemd" / "user" / BRIDGE_UNIT).is_file():
        say(f"ga vm enable-bridge: {BRIDGE_UNIT} is not installed — run `ga vm install --full` first")
        return 1
    if bridge_ready(home, say=lambda m: say("ga vm enable-bridge: " + m)) != 0:
        return 1
    r = runner or Runner()
    rc, out = r.run(["systemctl", "--user", "enable", "--now", BRIDGE_UNIT], timeout=60)
    say(f"ga vm enable-bridge: {BRIDGE_UNIT} enabled — send one test directive to to/{BRIDGE_NAME}" if rc == 0
        else f"ga vm enable-bridge: systemctl failed: {out.strip()}")
    return 0 if rc == 0 else 1


CONSOLE_URL = re.compile(r"GA Console: (http://127\.0\.0\.1:\d+/\S*)")


def url(home: Path, *, runner: Runner | None = None, user: str | None = None, host: str | None = None,
        say: Callable[[str], None] = print) -> int:
    """The console's current one-time URL (from the user journal) and the SSH tunnel line for another PC."""
    r = runner or Runner()
    _rc, out = r.run(["journalctl", "--user", "-u", CONSOLE_UNIT, "-n", "500", "--no-pager", "-o", "cat"], timeout=30)
    found = CONSOLE_URL.findall(out or "")
    if found:
        say(f"console: {found[-1]}")
        say(f"묻기:    {found[-1].split('#')[0]}#/ask")
    else:
        say("console: no URL in the journal yet — is it running? `systemctl --user status ga-console`")
    say("on the other PC (key login, no port opened on the VM):")
    say(f"  ssh -N -L {PORT}:127.0.0.1:{PORT} {user or getpass.getuser()}@{host or '<vm-ip>'}")
    say("  then open the URL above there (the same port, so the console's Host check passes)")
    return 0 if found else 1


# --- self-update (CMD-GA48) ----------------------------------------------------------------------------------------

UPDATE_LOG, NOTICED = "update.jsonl", "vm-noticed.json"
SHA_ASK, R0_ASK, R0_TIMEOUT = "vm-sha", "vm-r0", 3600  # DEV-VMSHA: notify/1 questions to the VM (from baseline-ops); R0 run cap, s


def last_update(home: Path) -> str:
    f = home / ".ga" / UPDATE_LOG
    try:
        lines = [x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


def _head(r: Runner, repo: Path) -> str:
    rc, out = _git(r, repo, "rev-parse", "HEAD")
    return out.strip() if rc == 0 else ""


def _installed_version(r: Runner, home: Path) -> str:
    py = home / "ga-venv" / "bin" / "python"
    if py.exists():
        rc, out = r.run([str(py), "-c", "import ga; print(ga.__version__)"], timeout=60)
        if rc == 0 and out.strip():
            return out.strip().splitlines()[-1]
    from .. import __version__
    return __version__


def _commit_url(sha: str) -> str:
    return f"https://github.com/cogito5170/ga-sdk/commit/{sha or 'HEAD'}"


def _mail(home: Path, kind: str, fid: str, sha: str, note: str) -> str:
    """One notify/1 to baseline-ops (fields: schema to kind ref id note); '' when sent, else 'failed: ...'."""
    from ..forms import dump_text
    from ..mailbox import MailError, Mailbox
    head = {"schema": "notify/1", "to": NOTICE_TO, "kind": kind, "id": fid, "ref": _commit_url(sha), "note": note[:280]}
    try:
        Mailbox(home / "baseline").send(NOTICE_TO, dump_text(head), sender=NOTICE_FROM)
    except MailError as e:
        return f"failed: {str(e)[:120]}"
    return ""


def _noticed(home: Path) -> dict[str, Any]:
    f = home / ".ga" / NOTICED
    try:
        d = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def _notice(r: Runner, home: Path, heads: dict[str, str]) -> str:
    """One notify/1 (ack) per new (ga version, ga-sdk SHA); 'sent', 'same', or 'failed: ...' (the next run retries)."""
    ver, sha = _installed_version(r, home), heads.get("ga-sdk") or ""
    st = _noticed(home)
    if st.get("version") == ver and (st.get("sha") or "") == sha:
        return "same"
    note = f"VM runs ga {ver} at {sha or '-'}; heads " + ", ".join(f"{k} {v[:12] or '-'}" for k, v in heads.items())
    err = _mail(home, "ack", f"vm-{(sha or ver)[:12]}", sha, note)
    if err:
        return err
    _write(home / ".ga" / NOTICED, json.dumps(dict(st, version=ver, sha=sha)) + "\n", lambda m: None)
    return "sent"


def _r0(r: Runner, home: Path, sha: str, force: bool = False) -> str:
    """DEV-VMSHA: the R0 test set (``pytest tests`` in ~/ga-sdk with the VM's venv) at ``sha``, once per SHA (or when
    asked); the counts, sha and env go to baseline-ops as one notify/1 report. No model is called: the suite is offline."""
    st = _noticed(home)
    if not force and st.get("r0_sha") == sha:
        return "same"
    py = home / "ga-venv" / "bin" / "python"
    rc, out = r.run([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=str(home / "ga-sdk"),
                    timeout=R0_TIMEOUT)
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|error|errors|skipped)", out.strip().splitlines()[-1]
                                               if out.strip() else "")}
    env = (r.run([str(py), "-V"], timeout=60)[1].strip() or "python ?") + f", {_os_name()}"
    res = (", ".join(f"{n} {k}" for k, n in counts.items()) or f"no summary (exit {rc})")
    err = _mail(home, "report", R0_ASK, sha, f"R0 tests at {sha}: {res}; exit {rc}; env {env}")
    if err:
        return err
    _write(home / ".ga" / NOTICED, json.dumps(dict(_noticed(home), r0_sha=sha)) + "\n", lambda m: None)
    return "sent"


def _requests(r: Runner, home: Path, heads: dict[str, str]) -> list[str]:
    """DEV-VMSHA: answer the notify/1 questions anyone with mailbox access left for the VM (``to/vm/``): ``vm-sha`` gets an ack naming
    the SHA now, ``vm-r0`` a fresh R0 run. Any other message is marked read and left alone; a failed answer stays unread."""
    from ..forms import parse_text
    from ..mailbox import Mailbox
    box, done = Mailbox(home / "baseline"), []
    ver, sha = _installed_version(r, home), heads.get("ga-sdk") or ""
    for m in box.unread(NOTICE_FROM):
        try:
            head = parse_text(m.text)[0]
        except Exception:  # noqa: BLE001 — not a form: nothing to answer
            head = {}
        ask = head.get("id") if m.valid and m.schema == "notify/1" and head.get("kind") == "question" else None
        if ask == SHA_ASK:
            err = _mail(home, "ack", SHA_ASK, sha, f"VM runs ga {ver} at {sha or '-'}")
        elif ask == R0_ASK:
            err = "" if _r0(r, home, sha, force=True) == "sent" else "failed"
        else:
            err = ""
        if err:
            done.append(f"{ask}: {err}")
            continue
        box.mark_read(NOTICE_FROM, m.path)
        if ask:
            done.append(f"{ask}: answered")
    return done


def update(home: Path, *, dry_run: bool = False, min_free_gb: float = MIN_FREE_GB, runner: Runner | None = None,
           free: Callable[[Any], int] = disk_free, branch: str = BRANCH, say: Callable[[str], None] = print,
           now: Callable[[], str] | None = None) -> int:
    """Fast-forward ~/ga-sdk, ~/baseline and ~/token to origin/<branch>; pip only when ga-sdk's HEAD or pins changed;
    restart only the enabled services whose code changed; one ack mail per new ga version. Never resets or forces."""
    from .. import events as EV
    sp = EV.span("SYSTEM", "vm-update", "update", dry_run=dry_run or None, branch=branch).start()
    try:
        rc = _update(home, sp, dry_run=dry_run, min_free_gb=min_free_gb, runner=runner, free=free, branch=branch,
                     say=say, now=now)
    except BaseException as e:
        sp.error(f"{type(e).__name__}: {e}")
        sp.fail()
        raise
    (sp.done if rc == 0 else sp.fail)(exit=rc)
    return rc


def _update(home: Path, sp: Any, *, dry_run: bool, min_free_gb: float, runner: Runner | None,
            free: Callable[[Any], int], branch: str, say: Callable[[str], None], now: Callable[[], str] | None) -> int:
    import time
    from .. import events as EV
    r = runner or Runner()
    d = check_disk(home, min_free_gb, free)
    if not d["ok"]:
        say(f"ga vm update: disk guard: {d['free_gb']} GB free < {d['min_gb']} GB — stopped before any fetch")
        sp.error("disk guard", free_gb=d["free_gb"], min_gb=d["min_gb"])
        return 1
    repos = {"ga-sdk": home / "ga-sdk", "baseline": home / "baseline", "token": home / "token"}
    before, after, skipped = {}, {}, []
    for name, repo in repos.items():
        if not repo.exists():
            continue
        before[name] = after[name] = _head(r, repo)
        try:
            plan_repo(r, repo)
            if dry_run:
                say(f"would: fetch + fast-forward {repo} to origin/{branch}")
                continue
            with EV.span("NET", "vm-update", "git fetch + ff", repo=name) as net:
                sync_repo(r, repo, "", branch, "update", lambda m: None)
                after[name] = _head(r, repo)
                net.done(before=(before[name] or "")[:12], after=(after[name] or "")[:12],
                         changed=before[name] != after[name])
        except VmError as e:
            skipped.append(f"{name}: {str(e)[:160]}")
            say(f"ga vm update: {e}")
    sdk_changed = before.get("ga-sdk") != after.get("ga-sdk")
    pip, restarted, mail, asked, r0 = False, [], "skipped", [], "skipped"
    venv, sdk = home / "ga-venv", home / "ga-sdk"
    rc = 0
    if dry_run:
        say("would: pip install only when ga-sdk HEAD or pins changed; restart the enabled services whose code changed")
        migrate_hub_model(home, dry_run=True, say=say)
        return 0
    migrate_hub_model(home, say=say)
    EV.emit("SYSTEM", "vm-update", "heads", "RUNNING", before={k: (v or "")[:12] for k, v in before.items()},
            after={k: (v or "")[:12] for k, v in after.items()})
    try:
        if "ga-sdk" in before:
            with EV.span("SYSTEM", "vm-update", "pip install") as ps:
                pip = _pip(r, home, venv, sdk, lambda m: None, by_head=True)
                ps.done(ran=pip)
    except VmError as e:
        skipped.append(f"pip: {e}")
        say(f"ga vm update: {e}")
        rc = 1
    if rc == 0 and sdk_changed:
        for unit in (CONSOLE_UNIT, BRIDGE_UNIT):
            if r.run(["systemctl", "--user", "is-enabled", unit], timeout=30)[0] == 0:  # never a disabled unit
                r.run(["systemctl", "--user", "restart", unit], timeout=60)
                restarted.append(unit)
                EV.emit("SYSTEM", "vm-update", "restart", "DONE", unit=unit)
    if rc == 0 and (home / "baseline").exists():
        mail = _notice(r, home, after)
        asked = _requests(r, home, after)
        r0 = _r0(r, home, after.get("ga-sdk") or "") if after.get("ga-sdk") else "skipped"
    line = {"at": (now or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))(),
            "heads": {"before": before, "after": after}, "pip": pip, "restarted": restarted, "skipped": skipped, "mail": mail}
    if asked or r0 != "skipped":
        line.update(asked=asked, r0=r0)
    log = home / ".ga" / UPDATE_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
    say("ga vm update: " + json.dumps(line, sort_keys=True))
    return rc
