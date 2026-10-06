"""``ga agy-agent install`` (CMD-GA41 S4): a tool-less agy plugin agent for plan turns.

agy reads Claude-plugin-form plugins: a directory with ``.claude-plugin/plugin.json`` and ``agents/<name>.md`` whose
front matter says ``tools: []``. With the backend option ``agent: <name>`` (agv) every turn runs as
``agy --agent <name> -p ...``: agy's fixed input per turn fell from 9,852 tokens (its default agent) to 2,530-2,958
(baseline, measured on the user's Mac). This command writes that directory and runs ``agy plugin install <dir>`` as an
argv list (never a shell); ``--print`` only writes the files and prints the command. No secret is read or written.

    ga agy-agent install [--name ga-plan] [--dir ~/.ga/agy-agents] [--cli agy] [--print]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .builtin import AGENT_NAME

DEFAULT_NAME = "ga-plan"
DEFAULT_DIR = "~/.ga/agy-agents"
INSTRUCTION = ("You answer with exactly one JSON block, in the form the prompt gives, and nothing else. "
               "You use no tools: you read no files and run no commands; everything you need is in the prompt.")


ASK_INSTRUCTION = ("You answer the user's question in plain, short sentences, in the language of the question, from the "
                   "context given in the prompt only. If the context does not say, you say you do not know; you never "
                   "invent facts. You use no tools: you read no files and run no commands.")


def files(name: str) -> dict[str, str]:
    """The plugin's files, relative path -> text."""
    manifest = {"name": name, "version": "1.0.0",
                "description": "GA Engine plan agent: no tools, one JSON block in the given form"}
    ask = name == "ga-ask"
    manifest["description"] = ("GA Engine ask agent: no tools, short answers from the given context only" if ask
                               else manifest["description"])
    desc = ("GA Engine ask turns. Short answers from the given context only; uses no tools." if ask else
            "GA Engine plan turns. Answers with one JSON block in the given form; uses no tools.")
    agent = f"---\nname: {name}\ndescription: {desc}\ntools: []\n---\n\n{ASK_INSTRUCTION if ask else INSTRUCTION}\n"
    return {".claude-plugin/plugin.json": json.dumps(manifest, indent=2) + "\n", f"agents/{name}.md": agent}


def write(name: str, root: Path) -> Path:
    if not AGENT_NAME.fullmatch(name):
        raise ValueError("--name must be [a-z0-9-], at most 40, starting with a letter or digit")
    d = root.expanduser() / name
    for rel, text in files(name).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


def install_argv(cli: list[str], plugin_dir: Path) -> list[str]:
    return list(cli) + ["plugin", "install", str(plugin_dir)]


def main(argv: list[str] | None = None, *, run=subprocess.run, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="ga agy-agent", description="a tool-less agy plugin agent for GA Engine plan "
                                 "turns (backend option agent: <name>)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="write the plugin directory and run agy plugin install <dir>")
    p.add_argument("--name", default=DEFAULT_NAME)
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--cli", default="agy", help="the agy command (one word, or a JSON list)")
    p.add_argument("--print", dest="print_only", action="store_true", help="write the files, print the command only")
    a = ap.parse_args(argv)
    cli = json.loads(a.cli) if a.cli.startswith("[") else [a.cli]
    try:
        d = write(a.name, Path(a.dir))
    except ValueError as e:
        print(f"ga agy-agent: {e}", file=sys.stderr)
        return 2
    cmd = install_argv(cli, d)
    out.write(f"wrote {d}\n")
    if a.print_only:
        out.write(" ".join(cmd) + "\n")
        out.write(f'then in ga-supervise.json: "options": {{"agent": "{a.name}"}}\n')
        return 0
    try:
        r = run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print(f"ga agy-agent: {cli[0]} not found; run it yourself: {' '.join(cmd)}", file=sys.stderr)
        return 1
    if r.returncode != 0:
        print(f"ga agy-agent: agy plugin install exited {r.returncode}", file=sys.stderr)
        return 1
    out.write(f'installed; in ga-supervise.json: "options": {{"agent": "{a.name}"}}\n')
    return 0


__all__ = ["DEFAULT_NAME", "DEFAULT_DIR", "INSTRUCTION", "ASK_INSTRUCTION", "files", "write", "install_argv", "main"]
