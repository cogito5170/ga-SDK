"""VM-BRIDGE-MODEL-1: which models a directive may name. For the agv backend the list is what ``agy models`` prints on
this machine (cached ``models_ttl_s``, default 1 h, in ``models_cache``); a bridge config ``models`` list replaces it
(and is the only source for other backends). A hand-written list in code is never used.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

SLUG = re.compile(r"^[a-z0-9][a-z0-9.-]{1,60}$")


def parse_agy_models(text: str) -> list[str]:
    """`agy models` lines are '<slug>  <description>': the slugs, in order."""
    out = []
    for line in text.splitlines():
        first = line.split()[0] if line.split() else ""
        if SLUG.match(first) and first not in out:
            out.append(first)
    return out


def available(cfg: dict[str, Any], backend: str, now: Callable[[], float] = time.time,
              run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[str]:
    """The models this bridge can run on ``backend``. Raises OSError / ValueError when it cannot tell."""
    if isinstance(cfg.get("models"), list):
        return [str(m) for m in cfg["models"]]
    if backend not in ("agv", "agy"):
        raise ValueError(f"no model list for backend {backend!r}: set models in the bridge config")
    cache = Path(cfg.get("models_cache") or "~/.ga/bridge/agy_models.json").expanduser()
    ttl = float(cfg.get("models_ttl_s", 3600))
    try:
        got = json.loads(cache.read_text(encoding="utf-8"))
        if now() - float(got["at"]) < ttl and got["models"]:
            return list(got["models"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    p = run([*(cfg.get("agy_cli") or ["agy"]), "models"], capture_output=True, text=True, timeout=60)
    models = parse_agy_models(p.stdout or "")
    if p.returncode != 0 or not models:
        raise ValueError(f"agy models gave no list (exit {p.returncode}): {(p.stderr or p.stdout or '').strip()[:160]}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"at": now(), "models": models}) + "\n", encoding="utf-8")
    return models


def check(cfg: dict[str, Any], model: str, backend: str, **kw: Any) -> str | None:
    """None when ``model`` may run on ``backend``, else the reason (for a declined report/2)."""
    try:
        names = available(cfg, backend, **kw)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return f"model {model!r} not checked: {str(e)[:150]}"
    if model not in names:
        return f"model {model!r} not in agy models" if backend in ("agv", "agy") and not isinstance(cfg.get("models"), list) \
            else f"model {model!r} not in the bridge's models"
    return None
