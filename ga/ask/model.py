"""``ga ask --model "..."`` (CMD-GA36 S2): one agy turn with tools off — the question and at most 1,500 tokens of
context (ctxpack's estimate), nothing more. Never used for routing.

Tools off: agy has no flag that removes its tools (AGY_FACTS: -p, --output-format, --model, --mode accept-edits|plan,
--disable-slash-commands, ...). The closest it offers is used: ``--mode plan`` (no edits, no commands run) and
``--disable-slash-commands``, the prompt says tools are off, and a turn in which agy still tried a tool is reported.
``--dangerously-skip-permissions`` is never passed.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from ..adapters import agy_cli
from ..ctxpack import tokens

PROMPT_MAX = 1500
TOOLS_OFF = ["--mode", "plan", "--disable-slash-commands"]
HEAD = ("You answer one question about the user's ga setup (GA Engine). Tools are off: do not read files or run "
        "commands; answer only from the context below, briefly, in the language of the question.\n")


ASK_AGENT = "ga-ask"
WARN_INPUT = 4000   # a tool-less agent measured 2,530-2,958 input tokens; agy's default agent 9,852 (BD-416)
UNKNOWN_AGENT = re.compile(r"unknown agent|agent\b.{0,60}\b(not found|not installed|does not exist|unknown)|"
                           r"no such agent|(not found|unknown|invalid)\b.{0,20}\bagent", re.I)


def install_hint(name: str = ASK_AGENT) -> str:
    return f"{name} 에이전트가 설치되어 있지 않아 모델 턴을 쓰지 않았습니다. 설치: ga agy-agent install --name {name}"


def input_warning(inp: int, name: str = ASK_AGENT) -> str | None:
    if inp > WARN_INPUT:
        return (f"입력이 큽니다({inp:,} 토큰) — 도구 없는 에이전트가 아닌 것 같습니다. "
                f"고치기: ga agy-agent install --name {name}")
    return None


def ask_cli(s: dict[str, Any]) -> list[str]:
    """settings agy_cli wins; else agy with the tool-less ask agent."""
    return list(s.get("agy_cli") or ["agy", "--agent", s.get("ask_agent") or ASK_AGENT])


def agent_installed(name: str, base: list[str] | None = None, timeout_s: float = 20.0) -> bool | None:
    """True/False from ``agy plugin list``; None when that could not be read (then the turn decides)."""
    try:
        p = subprocess.run(list(base or ["agy"]) + ["plugin", "list"], env=agy_cli.clean_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", p.stdout + p.stderr) is not None


def list_models(base: list[str] | None = None, timeout_s: float = 20.0) -> list[str] | None:
    """The slugs ``agy models`` prints (first word of each line); None when it could not be listed."""
    try:
        p = subprocess.run(list(base or ["agy"]) + ["models"], env=agy_cli.clean_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    out = [ln.split()[0] for ln in p.stdout.splitlines() if ln.strip() and re.fullmatch(r"[\w.\-]+", ln.split()[0])]
    return out or None


class ModelTurnError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def prompt(question: str, context: list[tuple[str, str]], cap: int = PROMPT_MAX) -> tuple[str, list[str]]:
    """The prompt: head, context parts in priority order (the last dropped first), the question. Under ``cap``."""
    q = question.strip()
    parts = list(context)
    dropped: list[str] = []

    def render(ps: list[tuple[str, str]], qq: str) -> str:
        ctx = "".join(f"## {name}\n{text.rstrip()}\n" for name, text in ps)
        return HEAD + "\n" + ctx + "\n## question\n" + qq + "\n"
    text = render(parts, q)
    while tokens(text) > cap and parts:
        dropped.append(parts.pop()[0])
        text = render(parts, q)
    while tokens(text) > cap:  # the question alone is too long: its end is cut
        q = q[: max(len(q) - 200, 0)]
        text = render(parts, q + " …")
    return text, dropped


def argv(cli: list[str], model: str, text: str) -> list[str]:
    return list(cli) + ["-p", text, "--output-format", "json", "--model", model] + TOOLS_OFF


def one_turn(text: str, *, cli: list[str] | None = None, model: str | None = None, cwd: str | None = None,
             timeout_s: float = 600.0) -> dict[str, Any]:
    """One agy process, stdin closed. Returns {text, usage, seconds, denied}; raises ModelTurnError."""
    from .store import SOLVE_DEFAULT, settings
    s = settings()
    cli = list(cli or ask_cli(s))
    model = model or s.get("ask_model") or SOLVE_DEFAULT["model"]
    if tokens(text) > PROMPT_MAX:
        raise ModelTurnError("prompt_over_cap")
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv(cli, model, text), cwd=cwd, env=agy_cli.clean_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        raise ModelTurnError("agy_not_found") from None
    except subprocess.TimeoutExpired:
        raise ModelTurnError("timeout") from None
    if p.returncode != 0 and UNKNOWN_AGENT.search(p.stdout + "\n" + p.stderr):
        raise ModelTurnError("agent_missing")
    o = agy_cli.parse_output(p.stdout, p.stderr)
    if p.returncode != 0 and agy_cli.CAPACITY.search(p.stdout + "\n" + p.stderr):
        raise ModelTurnError("capacity")
    if o.credits or o.quota:
        raise ModelTurnError("quota")
    if p.returncode != 0 or o.agy_error:
        raise ModelTurnError(f"exit_{p.returncode}")
    return {"text": o.text, "usage": o.usage or {}, "seconds": round(time.monotonic() - t0, 3),
            "denied": list(o.denied)}
