"""GA Actions: proposals, trial runs, the human approval and the registry (CMD-GA42 S2, S3).

    <GA_HOME>/actions/proposals/<name>.json   proposal + static check result + trial output (secrets withheld)
    <GA_HOME>/actions.json                    the registry {name: {argv, cwd, timeout_s, writes, network, about,
                                              approved_by, at, sha256}}
    <GA_HOME>/actions-policy.json             human-written: {"executables": [...], "auto_approve": [{argv_prefix,
                                              placeholders, cwd, writes, max_timeout_s}]}

GA_HOME is ``$GA_HOME`` or ``~/.ga``. A model may only PROPOSE. ``approve`` is called by a human act only — ``ga
actions approve`` on a TTY after y/N, or GA Console's POST after its token check (``approve(name, approver="console")``);
nothing read from mail, a report, a model turn or a proposal file approves anything. The one exception is a human-
written policy pattern, matched exactly. A registry entry whose sha256 does not match its fields is refused.

The sha256 is integrity, not authenticity: it catches an entry edited after approval, but anyone who can write
actions.json can also compute a matching sha256 for an entry they wrote by hand. So ``run`` never trusts an entry's
fields alone: at run time it re-runs the static check and refuses an argv that needs network unless the entry says
``network: true`` (CMD-GA42 rev 2), and it refuses a script whose bytes changed since the approval.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ..act.card import redact
from ..act.commands import NAME, clean_env
from . import check as C

OUT_CAP = 4000
TOOL_NEEDED = re.compile(r"^\s*TOOL_NEEDED:\s*(.+?)\s*$", re.M)
PROPOSE = re.compile(r"^PROPOSE[ \t]+(\S+)[ \t]*\n[ \t]*(\{.*\})[ \t]*$", re.M)
HASHED = ("argv", "cwd", "timeout_s", "writes", "network", "about", "files")


class ActionError(ValueError):
    pass


def home() -> Path:
    return Path(os.environ.get("GA_HOME") or "~/.ga").expanduser()


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def policy(h: Path | None = None) -> dict[str, Any]:
    p = _read((h or home()) / "actions-policy.json", {})
    return p if isinstance(p, dict) else {}


def executables(h: Path | None = None) -> list[str]:
    ex = policy(h).get("executables", [])
    return [x for x in ex if isinstance(x, str) and x] if isinstance(ex, list) else []


def proposal_path(name: str, h: Path | None = None) -> Path:
    if not NAME.match(name or ""):
        raise ActionError(f"action name {name!r}: [a-z][a-z0-9_-], up to 32")
    return (h or home()) / "actions" / "proposals" / f"{name}.json"


def _file_hashes(argv: list[str], root: Path) -> dict[str, str]:
    """sha256 of every argv element that is a file in the project (the script an approval covers)."""
    out = {}
    for a in argv:
        if a.startswith("-") or "{" in a:
            continue
        try:
            q = (Path(root) / a).resolve()
            if q.is_file() and Path(root).resolve() in q.parents:
                out[a] = hashlib.sha256(q.read_bytes()).hexdigest()
        except (OSError, ValueError):
            continue
    return out


def digest(entry: dict[str, Any]) -> str:
    body = json.dumps({k: entry.get(k) for k in HASHED}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ propose + trial

def _changed(work: Path) -> list[str]:
    p = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=work, capture_output=True,
                       text=True)
    return [ln[3:] for ln in p.stdout.splitlines() if ln[3:]]


def trial(p: dict[str, Any], root: Path) -> dict[str, Any]:
    """One run in a throwaway worktree (git worktree, else a copy without .git/.ga), with the timeout and capped,
    redacted output. Files written outside the proposal's ``writes`` are listed."""
    root = Path(root).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="ga-action-trial-"))
    work = tmp / "w"
    git = (root / ".git").exists()
    try:
        if git and subprocess.run(["git", "worktree", "add", "--detach", "--quiet", str(work), "HEAD"], cwd=root,
                                  capture_output=True).returncode == 0:
            pass
        else:
            git = False
            shutil.copytree(root, work, symlinks=True, ignore=shutil.ignore_patterns(".git", ".ga", "node_modules"))
        ex = p.get("example", {})
        if isinstance(ex, str):
            ex = {"path": ex} if "{path}" in p["argv"] else {"name": ex}
        used = {x.strip("{}") for x in C.PLACEHOLDERS if any(x in a for a in p["argv"])}
        argv, why = C.fill(p["argv"], {k: v for k, v in ex.items() if str(k).strip("{}") in used}, work)
        if argv is None:
            return {"ran": False, "reason": why}
        if "/" in argv[0]:
            argv[0] = str(work / argv[0])
        t0 = time.time()
        try:
            r = subprocess.run(argv, cwd=work / p.get("cwd", "."), env=clean_env(), capture_output=True, text=True,
                               timeout=float(p.get("timeout_s", 60)), stdin=subprocess.DEVNULL)
            code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            code, out = 124, f"timed out after {p.get('timeout_s', 60)} s"
        except OSError as e:
            code, out = 127, f"could not start: {type(e).__name__}"
        out, n = redact(out[-OUT_CAP:])
        res = {"ran": True, "exit": code, "seconds": round(time.time() - t0, 2), "output": out, "secrets_withheld": n}
        if git:
            from fnmatch import fnmatch
            wrote = _changed(work)
            res["wrote_outside"] = [f for f in wrote if not any(fnmatch(f, g) for g in p.get("writes", []))][:20]
        return res
    finally:
        if git:
            subprocess.run(["git", "worktree", "remove", "--force", str(work)], cwd=root, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def propose(p: dict[str, Any], root: Path, *, source: str, h: Path | None = None, run_trial: bool = True) -> dict[str, Any]:
    """Record a proposal: static check, then (if it passes and needs no network) one trial run. Returns the record.
    Whatever the proposal says, nothing here approves it."""
    h = h or home()
    name = str(p.get("name", "")) if isinstance(p, dict) else ""
    reasons, net = C.check(p, root, executables(h))
    rec: dict[str, Any] = {"schema": "ga-action-proposal/1", "name": name, "source": source[:80],
                           "root": str(Path(root).resolve()), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "proposal": {k: p.get(k) for k in sorted(C.FIELDS) if isinstance(p, dict) and k in p},
                           "check": {"ok": not reasons, "reasons": reasons, "network": net}}
    if reasons:
        rec["trial"] = {"ran": False, "reason": "refused by the static check"}
    elif net:
        rec["trial"] = {"ran": False, "reason": "needs network: only a human approval with network allowed admits it"}
    elif run_trial:
        rec["trial"] = trial({**p, "name": name}, root)
    if NAME.match(name):
        _write(proposal_path(name, h), rec)
        auto = _auto(rec, h)
        if auto:
            rec["auto_approved"] = _register(rec, auto, h)["approved_by"]
    return rec


def from_text(text: str, root: Path, *, source: str, h: Path | None = None, run_trial: bool = True) -> list[dict[str, Any]]:
    """PROPOSE blocks and TOOL_NEEDED lines in a model's text, each recorded as a proposal (a TOOL_NEEDED line has no
    argv, so it is recorded as refused until a person writes one). Approval words in the text mean nothing."""
    out = []
    for m in PROPOSE.finditer(text or ""):
        try:
            p = json.loads(m.group(2))
        except ValueError:
            p = {"argv": None}
        if not isinstance(p, dict):
            p = {"argv": None}
        out.append(propose({**p, "name": m.group(1)}, root, source=source, h=h, run_trial=run_trial))
    for line in TOOL_NEEDED.findall(text or ""):
        name = re.sub(r"[^a-z0-9_-]", "-", line.split(" - ")[0].strip().lower())[:32].strip("-_")
        if not NAME.match(name or ""):
            continue
        if any(r["name"] == name for r in out) or proposal_path(name, h or home()).exists():
            continue
        out.append(propose({"name": name, "argv": None, "why": line[:200]}, root, source=f"{source} TOOL_NEEDED",
                           h=h, run_trial=False))
    return out


# ------------------------------------------------------------------ approval + registry

def registry(h: Path | None = None) -> dict[str, Any]:
    r = _read((h or home()) / "actions.json", {})
    return r if isinstance(r, dict) else {}


def _auto(rec: dict[str, Any], h: Path) -> str | None:
    """The human policy pattern this proposal matches exactly, as 'policy:<i>', or None."""
    if not rec["check"]["ok"] or rec["check"]["network"]:
        return None
    p = rec["proposal"]
    for i, pat in enumerate(policy(h).get("auto_approve", []) or []):
        if not isinstance(pat, dict) or not isinstance(pat.get("argv_prefix"), list):
            continue
        pre, allowed = pat["argv_prefix"], pat.get("placeholders", [])
        argv = p.get("argv") or []
        if argv[:len(pre)] != pre or any(a not in allowed for a in argv[len(pre):]):
            continue
        if p.get("cwd", ".") != pat.get("cwd", ".") or list(p.get("writes", [])) != list(pat.get("writes", [])):
            continue
        if float(p.get("timeout_s", 60)) > float(pat.get("max_timeout_s", 600)):
            continue
        return f"policy:{i}"
    return None


def _register(rec: dict[str, Any], approver: str, h: Path, allow_network: bool = False) -> dict[str, Any]:
    p = rec["proposal"]
    entry = {"argv": list(p["argv"]), "cwd": p.get("cwd", ".") or ".", "timeout_s": float(p.get("timeout_s", 60)),
             "writes": list(p.get("writes", [])), "network": bool(rec["check"]["network"] and allow_network),
             "about": str(p.get("why", "") or "")[:120].replace("\n", " "),
             "files": _file_hashes(list(p["argv"]), Path(rec.get("root", ".")))}
    entry.update(approved_by=approver, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), sha256=digest(entry))
    reg = registry(h)
    reg[rec["name"]] = entry
    _write(h / "actions.json", reg)
    return entry


def approve(name: str, approver: str = "console", *, allow_network: bool = False, h: Path | None = None) -> dict[str, Any]:
    """The human act (CLI after a TTY y/N, or GA Console after its token check). Re-checks the proposal now; a
    refused one, or a network one without ``allow_network``, is not approved."""
    h = h or home()
    rec = _read(proposal_path(name, h), None)
    if not isinstance(rec, dict) or rec.get("name") != name:
        raise ActionError(f"no proposal named {name!r}")
    reasons, net = C.check({**rec.get("proposal", {}), "name": name}, Path(rec.get("root", ".")), executables(h))
    if reasons:
        raise ActionError(f"{name}: refused by the static check: {'; '.join(reasons)[:400]}")
    if net and not allow_network:
        raise ActionError(f"{name}: needs network; approve with network allowed or not at all")
    rec["check"] = {"ok": True, "reasons": [], "network": net}
    return _register(rec, str(approver)[:80] or "human", h, allow_network)


def revoke(name: str, h: Path | None = None) -> bool:
    h = h or home()
    reg = registry(h)
    if name not in reg:
        return False
    del reg[name]
    _write(h / "actions.json", reg)
    return True


def verified(h: Path | None = None) -> dict[str, dict[str, Any]]:
    """Registry entries whose sha256 matches; a tampered entry is left out."""
    return {n: e for n, e in registry(h).items()
            if NAME.match(n) and isinstance(e, dict) and e.get("sha256") == digest(e)}


def about(h: Path | None = None) -> dict[str, str]:
    """What a model sees: approved names and their one-line about."""
    return {n: e.get("about") or "(no description)" for n, e in sorted(verified(h).items())}


def run(name: str, values: dict[str, Any] | None, root: Path, *, h: Path | None = None) -> tuple[int, str]:
    """Run an approved action by name in ``root``: (exit code, capped redacted output). Raises ActionError when it
    is not approved, tampered, or its placeholders or argv do not pass the check now."""
    h = h or home()
    reg = registry(h)
    if name not in reg:
        raise ActionError(f"{name!r} is not an approved action")
    e = reg[name]
    if not isinstance(e, dict) or e.get("sha256") != digest(e):
        raise ActionError(f"{name!r}: registry entry does not match its sha256 (tampered); refused")
    root = Path(root).resolve()
    reasons, net = C.check({"name": name, "argv": e["argv"], "cwd": e["cwd"], "timeout_s": e["timeout_s"],
                            "writes": e["writes"], "example": {x.strip("{}"): "x" for x in C.PLACEHOLDERS
                                                               if any(x in a for a in e["argv"])}},
                           root, executables(h) + ([e["argv"][0]] if "/" not in e["argv"][0] else []))
    if reasons:
        raise ActionError(f"{name!r}: refused now: {'; '.join(reasons)[:300]}")
    if net and not e.get("network"):
        raise ActionError(f"{name!r}: needs network and the approval does not allow it")
    now = _file_hashes(list(e["argv"]), root)
    changed = [a for a, sha in (e.get("files") or {}).items() if now.get(a) != sha]
    if changed:
        raise ActionError(f"{name!r}: {', '.join(changed)} changed since the approval; a person approves it again")
    argv, why = C.fill(e["argv"], values or {}, root)
    if argv is None:
        raise ActionError(f"{name!r}: {why}")
    if "/" in argv[0]:
        argv[0] = str(root / argv[0])
    try:
        r = subprocess.run(argv, cwd=root / e["cwd"], env=clean_env(), capture_output=True, text=True,
                           timeout=float(e["timeout_s"]), stdin=subprocess.DEVNULL)
        code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        code, out = 124, f"timed out after {e['timeout_s']} s"
    return code, redact(out[-OUT_CAP:])[0]
