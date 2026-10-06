"""The model router for ``ga act --route`` (CMD-GA47): decide once, cheaply, which model an item starts on and how many
turns it gets, then allow at most ``climb`` rungs up (default 1).

    route = {difficulty 1-5, start: model, max_turns 1-10, source: item | ledger | triage | fallback}

Order: (a) the item's own ``route`` (ga plan writes it in the planning turn) when valid; (b) the outcome ledger
``<state>/routes.jsonl``: >= 3 past items of the same feature bucket succeeded -> the cheapest rung that succeeded for
all of them, 0 model calls; (c) one triage turn on ``triage_model`` (agent ga-plan, tools off) with a card of at most
CARD_MAX bytes - goal, file paths with sizes and kinds, done_when command names, test file names, never a file's
contents; an invalid or failed triage -> the cheapest rung with 3 turns (source fallback).

The cheapest rung's turn cap is min(max_turns, LOW_CAP) so it fails fast. Every outcome is one ledger row, so later
items of the same bucket need no triage. Standard library only; model names come only from the validated rung list.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from ..ctxpack import tokens
from ..net.pool import owned
from . import card as C
from . import retrieve as R

# the ladder, cheapest first (written out: the agy slug order is not a price order)
LADDER = ("gpt-oss-120b-medium",
          "gemini-3.6-flash-low", "gemini-3.6-flash-medium", "gemini-3.6-flash-high",
          "gemini-3.7-flash-low", "gemini-3.7-flash-medium", "gemini-3.7-flash-high",
          "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high",
          "gemini-3.1-pro-low", "gemini-3.1-pro-high",
          "claude-sonnet-5-5-low", "claude-sonnet-5-5-medium", "claude-sonnet-5-5-high",
          "claude-opus-5-5-low", "claude-opus-5-5-medium", "claude-opus-5-5-high")
TRIAGE_MODEL = "gemini-3.1-pro-high"
TRIAGE_AGENT = "ga-plan"
CARD_MAX = 2048          # bytes, UTF-8
LOW_CAP = 3              # the cheapest rung's turn cap
FALLBACK_TURNS = 3
LEDGER_MIN = 3           # successful past items of a bucket before the ledger routes
CLIMB, CLIMB_MAX = 1, 2
LEDGER = "routes.jsonl"
SOURCES = ("item", "ledger", "triage", "fallback")
_TEST = re.compile(r"(^|/)(test_[^/]*|[^/]*_test\.[^/]+|[^/]*\.(test|spec)\.[^/]+)$")
_JSON = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)
LEVELS = ("1 one obvious local edit (a constant, a typo, one line)",
          "2 a small change in one file with a clear failing test",
          "3 several functions or two files; some reading needed",
          "4 cross-file logic, a new feature with tests, unclear failures",
          "5 design work: many files, new interfaces, subtle bugs")


def rungs(models: list[str]) -> list[str]:
    """The configured models, cheapest first: LADDER order for the names it knows, the rest after in their order."""
    known = [m for m in LADDER if m in models]
    return known + [m for m in models if m not in LADDER]


def valid(r: Any, ladder: list[str]) -> dict[str, Any] | None:
    """{difficulty, start, max_turns} when every field is in range and start is a rung; else None."""
    if not isinstance(r, dict):
        return None
    d, s, t = r.get("difficulty"), r.get("start"), r.get("max_turns")
    if isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 5:
        return None
    if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= 10:
        return None
    if not isinstance(s, str) or s not in ladder:
        return None
    return {"difficulty": d, "start": s, "max_turns": t}


def fallback(ladder: list[str]) -> dict[str, Any]:
    return {"difficulty": 1, "start": ladder[0], "max_turns": FALLBACK_TURNS, "source": "fallback"}


def climb_of(v: Any) -> int:
    if v is None:
        return CLIMB
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= CLIMB_MAX:
        raise ValueError(f"climb must be 0..{CLIMB_MAX}")
    return v


def plan_rungs(route: dict[str, Any], ladder: list[str], climb: int) -> tuple[list[str], dict[str, int]]:
    """(the rungs to run: start and at most ``climb`` above it, their turn caps; the cheapest rung capped LOW_CAP)."""
    i = ladder.index(route["start"])
    run = ladder[i:i + 1 + climb]
    caps = {m: route["max_turns"] for m in run}
    if ladder[0] in caps:
        caps[ladder[0]] = min(route["max_turns"], LOW_CAP)
    return run, caps


# ------------------------------------------------------------------------------------------------ features + card
def _kind(path: str) -> str:
    suf = Path(path).suffix.lstrip(".").lower()
    return suf or "none"


def _done_names(raw: Any, done: list[str] | None, commands: dict[str, list[str]]) -> list[str]:
    """done_when as command names only: the item's name, the config command whose argv it is, or argv[0]'s base."""
    if isinstance(raw, str):
        return [raw]
    argv = raw if isinstance(raw, list) else done
    if not argv:
        return []
    for n, v in commands.items():
        if v == argv:
            return [n]
    return [Path(str(argv[0])).name[:40]]


def features(root: Path, raw: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """What the router may know about an item: paths, sizes, kinds, done_when names, test names - no contents."""
    root = Path(root)
    globs = [g for g in raw.get("files") or [] if isinstance(g, str)]
    repo = R.files(root, exts=None)
    own = [f for f in repo if owned(f, globs)]
    sizes = []
    for f in own:
        try:
            sizes.append((f, (root / f).stat().st_size))
        except OSError:
            sizes.append((f, 0))
    new = [g for g in globs if not any(ch in g for ch in "*?[") and g not in own]
    sizes += [(g, 0) for g in new]
    stems = {Path(f).stem for f, _ in sizes}
    tests = [f for f in repo if _TEST.search(f) and (f in own or any(s and s in Path(f).name for s in stems))]
    done = _done_names(raw.get("done_when"), cfg.get("done_when"), cfg.get("commands") or {})
    return {"files": sizes, "new": new, "tests": tests[:12], "done_when": done,
            "kinds": sorted({_kind(f) for f, _ in sizes})}


def _band(n: int, cuts: tuple[int, ...], names: tuple[str, ...]) -> str:
    for c, name in zip(cuts, names):
        if n <= c:
            return name
    return names[-1]


def bucket(f: dict[str, Any]) -> str:
    """files band | size band | kinds | done_when kind, e.g. ``f1|s<4k|py|test``."""
    n = len(f["files"])
    total = sum(s for _, s in f["files"])
    fb = _band(n, (1, 3, 8), ("f1", "f2-3", "f4-8", "f9+"))
    sb = _band(total, (4096, 16384, 65536), ("s<4k", "s<16k", "s<64k", "s64k+"))
    return "|".join([fb, sb, ",".join(f["kinds"]) or "none", ",".join(f["done_when"]) or "none"])


def _b(s: str) -> int:
    return len(s.encode("utf-8"))


def _cut(s: str, limit: int) -> str:
    if _b(s) <= limit:
        return s
    return s.encode("utf-8")[:max(0, limit - 9)].decode("utf-8", "ignore") + " ...(cut)"


def card(goal: str, f: dict[str, Any], ladder: list[str], limit: int = CARD_MAX) -> str:
    """The triage card (<= ``limit`` bytes): paths, sizes and names only - code never reads a file's text into it."""
    tail = ("## Models, cheapest first\n" + ", ".join(ladder) + "\n## Difficulty\n" + "\n".join(LEVELS) + "\n"
            'Answer ONE ```json block: {"difficulty": 1-5, "start": "<a model above>", "max_turns": 1-10, '
            '"why": "<one line>"}\n')
    tail = _cut(tail, limit // 2)
    head = "# ga act triage: pick the cheapest model likely to finish this item\n"
    goal_t = "## Goal\n" + _cut(C.redact(" ".join(str(goal).split()))[0], 500) + "\n"
    total = sum(s for _, s in f["files"])
    lines = ["## done_when: " + (", ".join(f["done_when"]) or "none"), "## Tests: " + (", ".join(f["tests"]) or "none"),
             f"## Files ({len(f['files'])}, {total} bytes; path bytes kind)"]
    lines += [f"{p} {s} {_kind(p)}" + (" new" if p in f["new"] else "") for p, s in f["files"]]
    room = limit - _b(head) - _b(goal_t) - _b(tail)
    body, used = [], 0
    for ln in lines:
        ln = _cut(ln, 300) + "\n"
        if used + _b(ln) > room:
            if used + 11 <= room:
                body.append("(more cut)\n")
            break
        body.append(ln)
        used += _b(ln)
    text = head + goal_t + "".join(body) + tail
    return _cut(text, limit)


def parse(answer: str, ladder: list[str]) -> tuple[dict[str, Any] | None, str]:
    """(the valid route or None, why)."""
    text = (answer or "").strip()
    m = _JSON.search(text)
    raw = m.group(1) if m else (text[text.find("{"):text.rfind("}") + 1] if "{" in text else "")
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, "triage: not JSON"
    r = valid(doc, ladder)
    if r is None:
        return None, "triage: invalid route (difficulty 1-5, start a listed model, max_turns 1-10)"
    why = doc.get("why") if isinstance(doc.get("why"), str) else ""
    return r, why[:200]


def triage(runner: Any, text: str, ladder: list[str]) -> tuple[dict[str, Any] | None, int, str]:
    """One triage turn: (route or None, tokens spent, why). A backend failure is a None route, never a crash."""
    from .loop import usage_counts
    try:
        out = runner.run_turn(text, None)
    except Exception as e:  # noqa: BLE001 - a failed triage falls back
        return None, tokens(text), f"triage failed: {type(e).__name__}"[:200]
    answer = getattr(out, "answer", out if isinstance(out, str) else "") or ""
    u = usage_counts(getattr(out, "usage", None), getattr(out, "usage_format", None))
    spent = sum(int(v or 0) for v in u.values()) if u else tokens(text) + tokens(answer)
    r, why = parse(answer, ladder)
    return r, spent, why


# ------------------------------------------------------------------------------------------------ ledger
def read(state: Path) -> list[dict[str, Any]]:
    p = Path(state) / LEDGER
    out = []
    if p.is_file():
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def append(state: Path, row: dict[str, Any]) -> None:
    Path(state).mkdir(parents=True, exist_ok=True)
    with (Path(state) / LEDGER).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def from_ledger(rows: list[dict[str, Any]], b: str, ladder: list[str]) -> dict[str, Any] | None:
    """>= LEDGER_MIN successful items of bucket ``b`` -> start at the cheapest rung that succeeded for all of them."""
    won = []
    for r in rows:
        used = r.get("rungs") if isinstance(r.get("rungs"), list) else []
        if r.get("bucket") == b and r.get("success") is True and used and used[-1] in ladder:
            won.append(r)
    if len(won) < LEDGER_MIN:
        return None
    start = max((r["rungs"][-1] for r in won), key=ladder.index)
    turns = [int(r.get("turns_last") or 0) for r in won if isinstance(r.get("turns_last"), int)]
    diffs = [r["route"]["difficulty"] for r in won if isinstance(r.get("route"), dict)
             and isinstance(r["route"].get("difficulty"), int)]
    return {"difficulty": max(1, min(5, round(sum(diffs) / len(diffs)))) if diffs else 1, "start": start,
            "max_turns": max(1, min(10, max(turns + [FALLBACK_TURNS]))), "source": "ledger"}


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per source and per bucket: items, start succeeded % (done on the start rung), climbs, mean tokens, triage."""
    def agg(rs: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rs)
        first = sum(1 for r in rs if r.get("success") is True and len(r.get("rungs") or []) == 1)
        climbs = sum(max(0, len(r.get("rungs") or []) - 1) for r in rs)
        tok = sum(int(r.get("tokens") or 0) for r in rs)
        tri = sum(int(r.get("triage_tokens") or 0) for r in rs)
        return {"items": n, "start_succeeded_pct": round(100.0 * first / n, 1) if n else 0.0, "climbs": climbs,
                "mean_tokens": round(tok / n, 1) if n else 0.0, "triage_tokens": tri}
    by_src: dict[str, list] = {}
    by_b: dict[str, list] = {}
    for r in rows:
        src = (r.get("route") or {}).get("source", "?") if isinstance(r.get("route"), dict) else "?"
        by_src.setdefault(str(src), []).append(r)
        by_b.setdefault(str(r.get("bucket", "?")), []).append(r)
    return {"all": agg(rows), "source": {k: agg(v) for k, v in sorted(by_src.items())},
            "bucket": {k: agg(v) for k, v in sorted(by_b.items())}}


def render(s: dict[str, Any]) -> str:
    L = [f"{'':<34} {'items':>5} {'start ok%':>9} {'climbs':>6} {'mean tok':>10} {'triage tok':>10}"]

    def row(name: str, a: dict[str, Any]) -> str:
        return (f"{name[:34]:<34} {a['items']:>5} {a['start_succeeded_pct']:>9} {a['climbs']:>6} "
                f"{a['mean_tokens']:>10} {a['triage_tokens']:>10}")
    L.append(row("all", s["all"]))
    L += ["by source"] + [row("  " + k, v) for k, v in s["source"].items()]
    L += ["by bucket"] + [row("  " + k, v) for k, v in s["bucket"].items()]
    return "\n".join(L)


# ------------------------------------------------------------------------------------------------ decide
def decide(root: Path, raw: dict[str, Any], cfg: dict[str, Any], state: Path, *,
           make_triage: Callable[[str], Any] | None, triage_model: str = TRIAGE_MODEL,
           compare: list[str] | None = None) -> dict[str, Any]:
    """{route (with source), bucket, triage_tokens, predictions?} for one item, before any work turn."""
    ladder = rungs(cfg["models"])
    if not ladder:
        raise ValueError("route: no models")
    f = features(root, raw, cfg)
    b = bucket(f)
    out: dict[str, Any] = {"bucket": b, "triage_tokens": 0}
    own = valid(raw.get("route"), ladder)
    if own:
        out["route"] = dict(own, source="item")
        return out
    led = from_ledger(read(state), b, ladder)
    if led:
        out["route"] = led
        return out
    models = list(compare) if compare else [triage_model]
    for m in models:
        if m not in ladder and m not in LADDER:
            raise ValueError(f"route: unknown triage model {m[:80]!r} (not in the models list)")
    text = card(str(raw.get("goal", "")), f, ladder)
    preds: dict[str, Any] = {}
    chosen: dict[str, Any] | None = None
    for i, m in enumerate(models):
        if make_triage is None:
            r, spent, why = None, 0, "triage: no runner"
        else:
            r, spent, why = triage(make_triage(m), text, ladder)
        out["triage_tokens"] += spent
        preds[m] = {"route": r, "tokens": spent, "why": why}
        if i == 0:
            chosen = r
    if compare:
        out["predictions"] = preds
    out["route"] = dict(chosen, source="triage") if chosen else fallback(ladder)
    out["card_bytes"] = _b(text)
    return out


__all__ = ["CARD_MAX", "CLIMB", "LADDER", "LEDGER", "LOW_CAP", "TRIAGE_MODEL", "append", "bucket", "card", "climb_of",
           "decide", "fallback", "features", "from_ledger", "parse", "plan_rungs", "read", "render", "rungs", "summary",
           "triage", "valid"]
