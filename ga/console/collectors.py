"""GA Console's collectors (CMD-CON2 S2): pure read functions over the paths in the console config.

- git through argv (``git -C <repo> …``), never a shell, with optional locks off (a read never takes a lock)
- the mailbox through ``ga.mailbox`` (fetch into a throw-away ref, ``git show`` of each head); nothing is marked read
- a work item's status is derived by code: the directive file (draft) -> its directive/2 on the mailbox (sent) -> the
  bridge naming it (running) -> a report/2 handling it (reported, or failed) -> the newest DECISION_LOG row naming it
  (integrated / sent back)
- every public function's answer passes ``clean``: a secret-looking string comes back as the withheld note
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..forms import FormError, hard, parse_text, validate
from ..mailbox import BRANCH, Mailbox, _safe, parse_path, secrets_in
from ..runlog import WITHHELD

BD = re.compile(r"\bBD-\d+\b")
CMD = re.compile(r"\b(?:CMD|ITEM)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b")
INTEGRATED = re.compile(r"integrat|merged|통합|병합", re.I)
SENT_BACK = re.compile(r"sent[ _]back|send back|되돌|반려|돌려보", re.I)
STATUSES = ("draft", "sent", "running", "reported", "integrated", "sent_back", "failed")
OPEN = ("sent", "running", "reported")


def clean(obj: Any) -> Any:
    """``obj`` with every secret-looking string replaced by the withheld note (rule R6's patterns, ga.mailbox)."""
    if isinstance(obj, str):
        return WITHHELD if secrets_in(obj) else obj
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj


def iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def mail_iso(utc: str) -> str | None:
    """20261005T010203.000004Z -> 2026-10-05T01:02:03+00:00."""
    try:
        return datetime.strptime(utc[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def first_sentence(text: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    m = re.match(r"(.+?(?:[.!?。]|다\.|요\.))(\s|$)", text)
    return (m.group(1) if m else text)[:limit]


# ---- git ------------------------------------------------------------------------------------------------------------

def git(repo: str | Path, *args: str, timeout: float = 15) -> str | None:
    """stdout of ``git -C repo args`` (argv, no shell), or None when git fails or the repo is missing."""
    if not Path(repo).is_dir():
        return None
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _integ_ref(path: str, ib: str | None) -> str | None:
    if not ib:
        return None
    for ref in (f"refs/remotes/origin/{ib}", f"refs/heads/{ib}"):
        if git(path, "rev-parse", "-q", "--verify", ref + "^{commit}") is not None:
            return ref
    return None


def _ahead_behind(path: str, base: str | None, ref: str) -> tuple[int | None, int | None]:
    if base is None:
        return None, None
    out = git(path, "rev-list", "--left-right", "--count", f"{base}...{ref}")
    if not out:
        return None, None
    behind, ahead = (int(x) for x in out.split())
    return ahead, behind


def repos(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in cfg.get("repos") or []:
        p, ib = r["path"], r.get("integration_branch")
        row: dict[str, Any] = {"name": r["name"], "path": p, "branch": None, "head": None, "head_subject": None,
                               "dirty": None, "integration_branch": ib, "behind": None, "ahead": None}
        head = git(p, "rev-parse", "HEAD")
        if head:
            row["head"] = head.strip()
            row["branch"] = (git(p, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
            row["head_subject"] = (git(p, "log", "-1", "--format=%s") or "").strip()
            st = git(p, "status", "--porcelain", "--untracked-files=no")
            row["dirty"] = None if st is None else bool(st.strip())
            row["ahead"], row["behind"] = _ahead_behind(p, _integ_ref(p, ib), "HEAD")
        out.append(row)
    return clean(out)


def branches(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in cfg.get("repos") or []:
        p, ib = r["path"], r.get("integration_branch")
        raw = git(p, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(subject)%00%(committerdate:iso-strict)",
                  "refs/heads", "refs/remotes/origin")
        if raw is None:
            continue
        base = _integ_ref(p, ib)
        seen: dict[str, dict[str, Any]] = {}
        for line in raw.splitlines():
            ref, sha, subject, at = (line.split("\0") + ["", "", ""])[:4]
            if ref.startswith("refs/heads/"):
                name, local = ref[len("refs/heads/"):], True
            else:
                name, local = ref[len("refs/remotes/origin/"):], False
            if name in ("HEAD", BRANCH) or (name in seen and not local):
                continue
            ahead, behind = _ahead_behind(p, base, ref)
            merged = None if base is None else git(p, "merge-base", "--is-ancestor", ref, base) is not None
            if name == ib:
                label = "통합 브랜치"
            elif merged:
                label = "통합됨"
            elif ahead is None:
                label = "통합 브랜치 없음"
            else:
                label = f"통합 안 됨 · 앞 {ahead} · 뒤 {behind}"
            seen[name] = {"repo": r["name"], "branch": name, "head": sha, "subject": subject, "at": at or None,
                          "merged_into_integration": merged, "ahead": ahead, "behind": behind, "label_ko": label}
        out.extend(sorted(seen.values(), key=lambda b: b["at"] or "", reverse=True))
    return clean(out)


# ---- the mailbox (read only) -----------------------------------------------------------------------------------------

@dataclass
class Head:
    path: str
    recipient: str
    sender: str
    utc: str
    form: str
    schema: str | None
    valid: bool
    head: dict[str, Any] = field(default_factory=dict)


class MailReader:
    """The mailbox branch's message heads, read through ``ga.mailbox.Mailbox`` (fetch at most every
    ``fetch_every_s``; a failed fetch falls back to the local ref). It never calls ``mark_read``."""

    def __init__(self, repo: str | Path | None, *, remote: str = "origin", name: str = "baseline",
                 fetch_every_s: float = 60.0, clock: Callable[[], float] = time.time):
        self.box = Mailbox(repo, remote=remote, quiet=True) if repo and Path(repo).is_dir() else None
        self.name, self.fetch_every_s, self.clock = name, float(fetch_every_s), clock
        self._tip: str | None = None
        self._at = -1e18
        self._heads: dict[str, Head] = {}
        self.lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: dict[str, Any], **kw: Any) -> "MailReader":
        mb = cfg.get("mailbox") or {}
        return cls(mb.get("repo"), remote=mb.get("remote") or "origin", name=mb.get("name") or "baseline",
                   fetch_every_s=float(mb.get("fetch_every_s", 60)), **kw)

    def tip(self, force: bool = False) -> str | None:
        if self.box is None:
            return None
        with self.lock:
            if force or self.clock() - self._at >= self.fetch_every_s:
                self._at = self.clock()
                try:
                    self._tip = self.box.tip()
                except Exception:  # offline: the last fetched ref, else the local one
                    for ref in (f"refs/remotes/{self.box.remote}/{BRANCH}", f"refs/heads/{BRANCH}"):
                        out = git(self.box.repo, "rev-parse", "-q", "--verify", ref)
                        if out:
                            self._tip = out.strip()
                            break
            return self._tip

    def heads(self) -> list[Head]:
        """Every message on the tip, oldest first (heads are cached by path: a message file never changes)."""
        tip = self.tip()
        if tip is None or self.box is None:
            return []
        out = []
        for path in self.box._files(tip):
            parts = parse_path(path)
            if not parts:
                continue
            h = self._heads.get(path)
            if h is None:
                recipient, utc, sender, fid = parts
                text = self.box._git("show", f"{tip}:{path}", check=False)
                try:
                    head, _ = parse_text(text)
                    valid = not hard(validate(head))
                except FormError:
                    head, valid = {}, False
                if secrets_in(text):
                    head, valid = {}, False
                h = Head(path, recipient, sender, utc, fid, head.get("schema"), valid, head)
                self._heads[path] = h
            out.append(h)
        return sorted(out, key=lambda h: h.utc)

    def unread(self, heads: list[Head]) -> int:
        """Unread for this console's name, the way ``Mailbox.scan`` counts it (its read set when this clone has one,
        else the messages after its own last send). Only reads the read set."""
        if self.box is None:
            return 0
        mine = [h for h in heads if h.recipient == self.name]
        try:
            exists = self.box.cursor_file(self.name).exists()
        except Exception:
            exists = False
        if exists:
            seen = self.box.read_set(self.name)
            return sum(h.path not in seen for h in mine)
        last = max((h.utc for h in heads if h.sender == self.name), default="")
        return sum(h.utc > last for h in mine)


def mail(reader: MailReader, limit: int = 50) -> list[dict[str, Any]]:
    hs = reader.heads()[::-1][:max(1, min(int(limit), 500))]
    return clean([{"path": h.path, "from": h.sender, "to": h.recipient, "form": h.form, "schema": h.schema,
                   "valid": h.valid, "at": mail_iso(h.utc)} for h in hs])


# ---- DECISION_LOG -----------------------------------------------------------------------------------------------------

def decision_rows(baseline: str | Path) -> list[dict[str, Any]]:
    """'| BD-n | text | BD-n |' rows of DECISION_LOG.md, newest first (the file's order)."""
    p = Path(baseline) / "DECISION_LOG.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        m = re.match(r"^\|\s*(BD-(\d+))\s*\|(.*)\|\s*(BD-\d+)?\s*\|?\s*$", line)
        if m:
            out.append({"id": m.group(1), "text": m.group(3).strip(), "n": int(m.group(2))})
    return out


def decisions(cfg: dict[str, Any], q: str = "") -> list[dict[str, Any]]:
    terms = [t.lower() for t in str(q or "").split() if t]
    rows = [r for r in decision_rows(cfg["baseline"]) if all(t in (r["id"] + " " + r["text"]).lower() for t in terms)]
    return clean(rows)


# ---- work -----------------------------------------------------------------------------------------------------------

def _read_head(p: Path) -> dict[str, Any]:
    try:
        head, _ = parse_text(p.read_text(encoding="utf-8"))
        return head
    except (OSError, FormError, UnicodeDecodeError):
        return {}


def _handled_ids(head: dict[str, Any]) -> list[str]:
    return [str(h.get("id")) for h in head.get("handled") or [] if isinstance(h, dict) and h.get("id")]


def _report_tokens(head: dict[str, Any]) -> dict[str, Any]:
    r = head.get("results") if isinstance(head.get("results"), dict) else {}
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
    return {"input": num(r.get("input_tokens")), "output": num(r.get("output_tokens")),
            "total": num(r.get("tokens")), "turns": num(r.get("turns")),
            "model": r.get("model") if isinstance(r.get("model"), str) else None}


def work(cfg: dict[str, Any], reader: MailReader | None = None, running: set[str] | None = None) -> list[dict[str, Any]]:
    base = Path(cfg["baseline"])
    heads = reader.heads() if reader else []
    sent = {h.form: h for h in heads if h.schema == "directive/2"}
    reports: dict[str, Head] = {}
    for h in heads:
        if str(h.schema or "").startswith("report/"):
            for i in _handled_ids(h.head) or [h.form]:
                reports[i] = h  # oldest first: the newest report wins
    rows = decision_rows(base)
    out = []
    items: list[tuple[str, str, Path, dict[str, Any]]] = []
    for p in sorted((base / "directives").glob("CMD-*.md")):
        head = _read_head(p)
        items.append(("directive", str(head.get("id") or p.stem), p, head))
    for p in sorted((base / "ops" / "agy_bridge" / "items").glob("*.json")):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            obj = {}
        items.append(("item", str((obj if isinstance(obj, dict) else {}).get("id") or p.stem), p,
                      obj if isinstance(obj, dict) else {}))
    for kind, wid, p, head in items:
        d = sent.get(_safe(wid, "form"))
        rep = reports.get(wid)
        mentions = [r for r in rows if re.search(r"(?<![A-Za-z0-9-])" + re.escape(wid) + r"(?![A-Za-z0-9])", r["text"])]
        status = "draft" if kind == "directive" else "sent"
        if kind == "item" and head.get("status") in STATUSES:
            status = head["status"]
        if d is not None:
            status = "sent"
        if running and wid in running and status in ("draft", "sent"):
            status = "running"
        if rep is not None:
            st = {str(h.get("id")): h.get("status") for h in rep.head.get("handled") or [] if isinstance(h, dict)}
            status = "failed" if st.get(wid) in ("failed", "blocked") else "reported"
        for r in mentions:  # newest first: the first row that settles it
            if SENT_BACK.search(r["text"]):
                status = "sent_back"
                break
            if INTEGRATED.search(r["text"]):
                status = "integrated"
                break
        goal = head.get("goal") or head.get("title") or ""
        tok = _report_tokens(rep.head) if rep else {}
        branch = None
        if rep is not None:
            commits = rep.head.get("commits") or []
            if commits and isinstance(commits[0], dict):
                branch = commits[0].get("branch")
        out.append({
            "id": wid, "title": first_sentence(head.get("title") or goal, 120), "to": head.get("to"),
            "kind": kind, "status": status, "sent_at": mail_iso(d.utc) if d else None,
            "reported_at": mail_iso(rep.utc) if rep else None,
            "tokens": tok.get("total") if tok.get("total") is not None else
            (None if not tok or tok.get("input") is None else (tok.get("input") or 0) + (tok.get("output") or 0)),
            "bd": [r["id"] for r in mentions],
            "summary_ko": first_sentence(mentions[0]["text"]) if mentions else first_sentence(goal),
            "paths": {"directive": str(p.relative_to(base)) if kind == "directive" else (d.path if d else None),
                      "report": rep.path if rep else None,
                      "item": str(p.relative_to(base)) if kind == "item" else None},
            "branch": branch})
    return clean(out)


# ---- tokens ---------------------------------------------------------------------------------------------------------

def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or "").strip().replace(",", "").replace("~", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmM]?)$", s)
    if not m:
        return None
    return int(float(m.group(1)) * {"": 1, "k": 1000, "m": 1_000_000}[m.group(2).lower()])


def _float(v: Any) -> float | None:
    try:
        return float(str(v).strip().lstrip("$").replace(",", ""))
    except ValueError:
        return None


def _row(source: str, rid: str, model: Any, turns: Any, i: Any, o: Any, cr: Any, cost: Any, at: Any,
         total: Any = None) -> dict[str, Any]:
    i, o, cr = _int(i), _int(o), _int(cr)
    tot = _int(total)
    if tot is None and (i is not None or o is not None):
        tot = (i or 0) + (o or 0) + (cr or 0)
    return {"source": source, "id": str(rid), "model": model if isinstance(model, str) else None, "turns": _int(turns),
            "input": i, "output": o, "cache_read": cr, "total": tot, "cost_usd": _float(cost) if cost not in
            (None, "") else None, "at": at}


def _jsonl(p: Path) -> list[dict[str, Any]]:
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if isinstance(r, dict):
                    out.append(r)
            except ValueError:
                pass
    except OSError:
        pass
    return out


def _at(v: Any, fallback: str | None) -> str | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return iso(float(v))
    return str(v) if v else fallback


def rw1_rows(baseline: str | Path) -> list[dict[str, Any]]:
    """research/realwork/RW1_HUB_LEDGER.md: 작업 | 역할 | 모델 | 판정 되돌림 | 사람 개입 | input | output | cache read |
    cache write | cost_usd | 최종 | BD."""
    p = Path(baseline) / "research" / "realwork" / "RW1_HUB_LEDGER.md"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 10 or cells[0] in ("작업", "") or set(cells[0]) <= set("-: "):
            continue
        out.append(_row("rw1", cells[0], cells[2], None, cells[5], cells[6], cells[7], cells[9], None))
    return out


def tokens(cfg: dict[str, Any], reader: MailReader | None = None, *, ask_home: str | Path | None = None,
           clock: Callable[[], float] = time.time) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for d in (cfg.get("token_sources") or {}).get("act", []):
        for f in sorted(Path(d).glob("*.jsonl")) if Path(d).is_dir() else []:
            by: dict[str, list[dict[str, Any]]] = {}
            for r in _jsonl(f):
                by.setdefault(str(r.get("item") or f.stem), []).append(r)
            for item, rs in by.items():
                s = lambda k: sum(_int(r.get(k)) or 0 for r in rs)  # noqa: E731
                rows.append(_row("act", item, rs[-1].get("model"), len(rs), s("input"), s("output"), s("cache_read"),
                                 None, _at(rs[-1].get("at"), f.stem)))
    for f in (cfg.get("token_sources") or {}).get("supervise", []):
        for r in _jsonl(Path(f)):
            rows.append(_row("supervise", r.get("directive") or r.get("id") or r.get("run") or "supervise",
                             r.get("model"), r.get("turns"), r.get("input", r.get("input_tokens")),
                             r.get("output", r.get("output_tokens")), r.get("cache_read"), r.get("cost_usd"),
                             _at(r.get("at"), r.get("date"))))
    for h in (reader.heads() if reader else []):
        if str(h.schema or "").startswith("report/") and isinstance(h.head.get("results"), dict):
            t = _report_tokens(h.head)
            if t["input"] is None and t["total"] is None:
                continue
            rows.append(_row("bridge", (_handled_ids(h.head) or [h.form])[0], t["model"], t["turns"], t["input"],
                             t["output"], None, None, mail_iso(h.utc), total=t["total"]))
    rows.extend(rw1_rows(cfg["baseline"]))
    from ..ask.store import DayTurns, home as ask_default_home, today as ask_today
    h = Path(ask_home or cfg.get("ask_home") or ask_default_home())
    try:
        turns = DayTurns(h, clock=clock).used() if h.is_dir() else 0
    except Exception:
        turns = 0
    day = ask_today(clock)
    todays = [r for r in rows if str(r["at"] or "").startswith(day)]
    return clean({"today": {"agy_turns": turns, "input": sum(r["input"] or 0 for r in todays),
                            "output": sum(r["output"] or 0 for r in todays),
                            "total": sum(r["total"] or 0 for r in todays)},
                  "rows": rows})


# ---- state ----------------------------------------------------------------------------------------------------------

def bridge_state(cfg: dict[str, Any], services: Any = None) -> dict[str, Any]:
    b = cfg.get("bridge") or {}
    snap = services.get("bridge") if services is not None and "bridge" in services.names() else None
    last = services.last_line("bridge", prefix="bridge:") if snap else None
    return {"running": bool(snap and snap["state"] in ("starting", "running")),
            "last_seen": last["at"] if last else None, "last_message": last["text"] if last else None,
            "config_path": b.get("config_path") or None}


def state(cfg: dict[str, Any], reader: MailReader | None = None, services: Any = None,
          clock: Callable[[], float] = time.time) -> dict[str, Any]:
    svc = services.snapshot() if services is not None else []
    running = services.running_ids() if services is not None else set()
    w = work(cfg, reader, running)
    heads = reader.heads() if reader else []
    from .. import __version__
    return clean({"now": iso(clock()), "version": __version__, "bridge": bridge_state(cfg, services), "repos": repos(cfg), "services": svc,
                  "counts": {"work_open": sum(x["status"] in OPEN for x in w),
                             "mail_unread": reader.unread(heads) if reader else 0,
                             "services_running": sum(s["state"] == "running" for s in svc)}})


def fingerprint(cfg: dict[str, Any], reader: MailReader | None = None) -> dict[str, Any]:
    """What changed is found by code: the mtimes of the read files, each repo's HEAD and refs, the mailbox tip."""
    base = Path(cfg["baseline"])
    files = [base / "DECISION_LOG.md", base / "research" / "realwork" / "RW1_HUB_LEDGER.md"]
    files += sorted((base / "directives").glob("CMD-*.md")) + sorted((base / "ops" / "agy_bridge" / "items").glob("*.json"))
    mt = {}
    for f in files:
        try:
            mt[str(f)] = f.stat().st_mtime_ns
        except OSError:
            pass
    heads = {}
    for r in cfg.get("repos") or []:
        heads[r["name"]] = (git(r["path"], "rev-parse", "HEAD") or "").strip()
        refs = git(r["path"], "for-each-ref", "--format=%(objectname)", "refs/heads", "refs/remotes/origin")
        heads[r["name"] + ":refs"] = str(hash(refs))
    return {"files": mt, "heads": heads, "mail": reader.tip() if reader else None}
