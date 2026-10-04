"""ga's backend plugins (CMD-GA28 S2): entry point group ``ga.backends``, loaded by the same rules as rlo.plugins.

    [project.entry-points."ga.backends"]   my_host = "my_pkg.host:BACKEND"

    from ga import backends
    runner = backends.create("claude_cli", "claude-haiku-4-5", {}, {"cwd": ".", "timeout_s": 600})
    python -m ga.backends list          # what loaded, what did not, and each one's fixed overhead

A plugin whose import fails, whose ``api`` is not ``API_VERSION``, whose ``name`` differs from its entry point, that
lacks an attribute, or whose name is already taken is recorded in ``load_errors()`` and left out; the rest load. The
built-ins load the same way (as entry points), agv first; ``agy`` is an alias of ``agv``.
"""
from __future__ import annotations

import json
import sys
from importlib import metadata
from typing import Any

from .base import API_VERSION, ConfigError

GROUP = "ga.backends"
REQUIRED = ("name", "version", "api", "create", "overhead")
BUILTINS = {  # agv first (S2)
    "agv": "ga.backends.builtin:AGV",
    "gemini_cli": "ga.backends.builtin:GEMINI_CLI",
    "claude_cli": "ga.backends.builtin:CLAUDE_CLI",
    "codex_cli": "ga.backends.builtin:CODEX_CLI",
    "openai_http": "ga.backends.http:OPENAI_HTTP",
    "anthropic_http": "ga.backends.http:ANTHROPIC_HTTP",
}
ALIASES = {"agy": "agv"}  # the Antigravity CLI's command is agy (S3); the user calls the host agv


class Registry:
    def __init__(self) -> None:
        self.plugins: dict[str, Any] = {}
        self.origin: dict[str, str] = {}
        self.errors: list[dict[str, str]] = []

    def add(self, ep: Any) -> None:
        """Load one entry point. A failure is recorded only — it never stops another plugin."""
        name, value = ep.name, ep.value
        try:
            obj = ep.load()
            if isinstance(obj, type):
                obj = obj()
            for attr in REQUIRED:
                if not hasattr(obj, attr):
                    raise TypeError(f"missing {attr!r}")
            if obj.name != name:
                raise ValueError(f"name {obj.name!r} differs from the entry point name {name!r}")
            if obj.api != API_VERSION:
                raise ValueError(f"backend API {obj.api!r}, ga speaks {API_VERSION}")
            if name in self.plugins or name in ALIASES:
                raise ValueError(f"duplicate name (already from {self.origin.get(name, 'an alias')})")
        except Exception as e:  # import · shape · version · duplicate: only this one is left out
            self.errors.append({"group": GROUP, "name": name, "value": value, "error": f"{type(e).__name__}: {e}"[:300]})
            return
        self.plugins[name] = obj
        dist = getattr(getattr(ep, "dist", None), "name", None)
        self.origin[name] = value + (f" ({dist})" if dist else "")

    def get(self, name: str) -> Any:
        name = ALIASES.get(name, name)
        try:
            return self.plugins[name]
        except KeyError:
            why = [e["error"] for e in self.errors if e["name"] == name]
            raise KeyError(f"no {GROUP} plugin {name!r}" + (f" ({why[0]})" if why else "")) from None

    def listing(self) -> dict[str, Any]:
        return {"api": API_VERSION, "aliases": dict(ALIASES),
                "backends": {n: {"version": str(p.version), "from": self.origin[n], "overhead": dict(p.overhead)}
                             for n, p in self.plugins.items()},
                "errors": list(self.errors)}


def _entry_points() -> list[Any]:
    eps = metadata.entry_points()
    return list(eps.select(group=GROUP) if hasattr(eps, "select") else eps.get(GROUP, []))


def load() -> Registry:
    """The built-ins first (agv first), then installed entry points by name. ga-sdk's own entry point for a built-in
    loads once."""
    r = Registry()
    for name, value in BUILTINS.items():
        r.add(metadata.EntryPoint(name, value, GROUP))
    for ep in sorted(_entry_points(), key=lambda e: (e.name, e.value)):
        if BUILTINS.get(ep.name) == ep.value:
            continue
        r.add(ep)
    return r


_REGISTRY: Registry | None = None


def registry(reload: bool = False) -> Registry:
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = load()
    return _REGISTRY


def get(name: str) -> Any:
    return registry().get(name)


def load_errors() -> list[dict[str, str]]:
    return list(registry().errors)


def create(name: str, model: str, options: dict[str, Any] | None = None, ctx: dict[str, Any] | None = None) -> Any:
    """A runner of backend ``name`` on ``model``. KeyError: no such backend; ConfigError: the model or options."""
    return get(name).create(model, dict(options or {}), dict(ctx or {}))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] != ["list"]:
        print("usage: python -m ga.backends list", file=sys.stderr)
        return 2
    print(json.dumps(registry().listing(), ensure_ascii=False, indent=1))
    return 0


__all__ = ["GROUP", "BUILTINS", "ALIASES", "Registry", "load", "registry", "get", "create", "load_errors",
           "ConfigError", "API_VERSION"]
