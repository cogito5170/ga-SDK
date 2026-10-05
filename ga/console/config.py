"""``~/.ga/console.json`` (CMD-CON2 S3): the paths the collectors read and the services the console may start.

    {"schema": "ga-console/1",
     "baseline": "~/baseline",                                   directives/, ops/agy_bridge/items/, DECISION_LOG.md, …
     "repos": [{"name", "path", "integration_branch"}],          git checkouts shown on the state and branches pages
     "mailbox": {"repo", "remote", "fetch_every_s", "name"},     the ga-mailbox branch (read only: nothing marked read)
     "ask_home": null,                                           ga ask's folder (default $GA_ASK_HOME or ~/.ga-ask)
     "token_sources": {"act": [dirs], "supervise": [files]},     ledgers read by /api/tokens
     "bridge": {"argv", "cwd", "config_path"},                   the agy bridge, run as the service named "bridge"
     "services": {name: {"argv", "cwd", "env_file"?, "health_url"?, "port"?}}}

argv is a list (never a shell string); ``~`` is expanded in paths and argv words. An env file's values are loaded into
that child's environment only: the console never logs, returns or writes them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMA = "ga-console/1"
DEFAULT_PATH = "~/.ga/console.json"
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


class ConfigError(ValueError):
    pass


def default(home: str = "~") -> dict[str, Any]:
    """The Mac layout: ~/baseline, ~/token (Token README path B), ~/ga-sdk-check, the bridge with ~/agy-bridge.json."""
    h = home.rstrip("/")
    venv = f"{h}/token/.venv/bin"  # README path B: python3.12 -m venv .venv at the checkout root
    tok_env = f"{h}/token/.env"
    return {
        "schema": SCHEMA,
        "baseline": f"{h}/baseline",
        "repos": [{"name": "baseline", "path": f"{h}/baseline", "integration_branch": "claude/gracious-meitner-vp49xe"},
                  {"name": "token", "path": f"{h}/token", "integration_branch": "claude/gracious-meitner-vp49xe"},
                  {"name": "ga-sdk", "path": f"{h}/ga-sdk-check", "integration_branch": "claude/gracious-meitner-vp49xe"}],
        "mailbox": {"repo": f"{h}/baseline", "remote": "origin", "fetch_every_s": 60, "name": "baseline"},
        "ask_home": None,
        "token_sources": {"act": [f"{h}/ga-sdk-check/.ga/act/ledger", f"{h}/token/.ga/act/ledger"],
                          "supervise": [f"{h}/ga-sdk-check/.ga/ledger.jsonl"]},
        "bridge": {"argv": ["python3", f"{h}/baseline/ops/agy_bridge/bridge.py", "--config", f"{h}/agy-bridge.json"],
                   "cwd": f"{h}/baseline", "config_path": f"{h}/agy-bridge.json"},
        "services": {
            "token-api": {"argv": [f"{venv}/uvicorn", "app.main:create_app", "--factory", "--port", "8000"],
                          "cwd": f"{h}/token/backend", "env_file": tok_env,
                          "health_url": "http://127.0.0.1:8000/healthz", "port": 8000},
            "token-worker": {"argv": [f"{venv}/python", "-m", "app.worker"], "cwd": f"{h}/token/backend",
                             "env_file": tok_env, "ready_line": "worker ready"},
            "token-web": {"argv": ["npm", "run", "dev"], "cwd": f"{h}/token/frontend", "env_file": tok_env,
                          "health_url": "http://127.0.0.1:3000", "port": 3000},
        },
    }


def vm(home: str = "~") -> dict[str, Any]:
    """The VM layout (CMD-OPS1): the engine only. ~/baseline and ~/ga-sdk, no Token stack, no bridge, no services."""
    h = home.rstrip("/")
    br = "claude/gracious-meitner-vp49xe"
    return {
        "schema": SCHEMA,
        "baseline": f"{h}/baseline",
        "repos": [{"name": "baseline", "path": f"{h}/baseline", "integration_branch": br},
                  {"name": "ga-sdk", "path": f"{h}/ga-sdk", "integration_branch": br}],
        "mailbox": {"repo": f"{h}/baseline", "remote": "origin", "fetch_every_s": 60, "name": "baseline"},
        "ask_home": None,
        "token_sources": {"act": [f"{h}/ga-sdk/.ga/act/ledger"], "supervise": [f"{h}/ga-sdk/.ga/ledger.jsonl"]},
        "bridge": None,
        "services": {},
    }


def _x(s: Any) -> str:
    return str(Path(str(s)).expanduser()) if str(s).startswith("~") else str(s)


def check(cfg: Any) -> dict[str, Any]:
    """The config with ``~`` expanded, or ConfigError naming what is wrong."""
    if not isinstance(cfg, dict) or cfg.get("schema", SCHEMA) != SCHEMA:
        raise ConfigError(f"not a {SCHEMA} config")
    if not cfg.get("baseline"):
        raise ConfigError("baseline: the path of the baseline checkout is required")
    out = {"repos": [], "mailbox": {}, "ask_home": None, "token_sources": {}, "bridge": None, "services": {}, **cfg}
    out["baseline"] = _x(out["baseline"])
    repos = []
    for r in out.get("repos") or []:
        if not isinstance(r, dict) or not NAME.match(str(r.get("name", ""))) or not r.get("path"):
            raise ConfigError(f"repos: each needs a name and a path ({r!r})")
        repos.append({"name": r["name"], "path": _x(r["path"]), "integration_branch": r.get("integration_branch")})
    out["repos"] = repos
    mb = dict(out.get("mailbox") or {})
    if mb.get("repo"):
        mb["repo"] = _x(mb["repo"])
    out["mailbox"] = mb
    if out.get("ask_home"):
        out["ask_home"] = _x(out["ask_home"])
    ts = out.get("token_sources") or {}
    out["token_sources"] = {k: [_x(p) for p in (ts.get(k) or [])] for k in ("act", "supervise")}
    services = {}
    for name, s in (out.get("services") or {}).items():
        if name == "bridge":
            raise ConfigError("services: 'bridge' is the bridge's own name (configure it under bridge)")
        services[name] = _service(name, s)
    if out.get("bridge"):
        b = out["bridge"]
        services["bridge"] = _service("bridge", {k: v for k, v in b.items() if k != "config_path"})
        out["bridge"] = {**services["bridge"], "config_path": _x(b.get("config_path") or "")}
    out["services"] = services
    return out


def _service(name: str, s: Any) -> dict[str, Any]:
    if not NAME.match(str(name)):
        raise ConfigError(f"service name {name!r} is not a name")
    if not isinstance(s, dict):
        raise ConfigError(f"service {name}: not an object")
    argv = s.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise ConfigError(f"service {name}: argv must be a non-empty list of strings (no shell)")
    out = {"argv": [_x(a) for a in argv], "cwd": _x(s.get("cwd") or "."),
           "env_file": _x(s["env_file"]) if s.get("env_file") else None,
           "health_url": s.get("health_url") or None, "port": int(s["port"]) if s.get("port") else None,
           "ready_line": s.get("ready_line") or None}
    if out["health_url"] and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", out["health_url"]):
        raise ConfigError(f"service {name}: health_url must be http://127.0.0.1 or localhost")
    return out


def load(path: str | Path = DEFAULT_PATH) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"{p} not found — run `ga console init` first")
    try:
        return check(json.loads(p.read_text(encoding="utf-8")))
    except ValueError as e:
        raise ConfigError(f"{p}: {e}") from None


def init(path: str | Path = DEFAULT_PATH, *, force: bool = False) -> Path:
    """Write the default config (paths written with ``~``, expanded at load). Refuses to overwrite unless ``force``."""
    p = Path(path).expanduser()
    if p.exists() and not force:
        raise ConfigError(f"{p} exists (give --force to overwrite)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(default(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def read_env_file(path: str | Path) -> dict[str, str]:
    """KEY=VALUE lines (``export`` and quotes allowed, # comments). The values go to a child's env only."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, sep, v = line.partition("=")
        k = k.strip()
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out
