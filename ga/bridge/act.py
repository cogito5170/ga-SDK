"""``ga bridge`` code work (VM-BRIDGE-ACT-1): a directive/2 whose mail carries a ```ga-act block runs ``ga act`` in a
separate git worktree instead of ``ga supervise`` (whose tools only read). Ported from baseline's
ops/agy_bridge/act_runner.py (BD-424 item -> ga act, BD-450 rewrite, BD-457 push agv/<id>-r<rev>, BD-463 repo choice);
the item rides in the mail itself, so the VM needs no pull of baseline's checkout.

The block, after the directive's head (baseline writes it; the model may not edit the tests):

    ```ga-act
    {"item":     {"id": "CMD-X1", "goal": "...", "files": ["pkg/mod.py"], "done_when": "test"},
     "tests":    {"tests/test_x1.py": "<file text>"},
     "commands": {"commands": {"test": ["{venv_python}", "-m", "pytest", "-q", "tests/test_x1.py"]}, "timeout_s": 600},
     "base":     "main",                    # the branch to start from (default: the checkout's origin HEAD)
     "repo":     "token",                   # optional: a name in the bridge config's act.repos (default: act.repo)
     "rewrite":  ["pkg/mod.py"],            # optional: removed in the worktree first (one of the item's files)
     "ladder":   ["m1", "m2"]}              # optional: cheap model first, the next only when blocked
    ```

One run: fetch the base, ``git worktree add -B agv/<id>`` next to the checkout, write the tests, ``python -m ga act``,
commit on agv/<id>, push it as agv/<id>-r<rev> when the run is met (never with force; only a real patch that passed
the secret check),
remove the worktree, and answer report/2 with the status, served model, turns, tokens, commit and patch (capped).
Placeholders in command argv: {venv_python} = the checkout's .venv python, {checkout} = the checkout, {python} = this ga's
python. The bridge config's ``act`` block: repo (required), repo_name, repos {name: {path, repo_name}}, backend (agv),
model, options, max_turns (10), timeout_s (3600), push (true), remote (origin), state_dir.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from .. import events as EV
from ..mailbox import secrets_in

BLOCK = re.compile(r"```ga-act[ \t]*\n(.*?)\n```", re.S)
PATCH_CAP = 60_000
SAFE_PATH = re.compile(r"^(?!/)(?!.*\.\.)[\w./@()\[\]-]+$")
BRANCH = re.compile(r"^[\w./-]+$")
SPEC_KEYS = {"item", "tests", "commands", "base", "repo", "rewrite", "ladder", "model", "max_turns"}


def item_spec(body: str) -> dict[str, Any] | None:
    """The ```ga-act block of a mail body: None when there is none; ValueError when it is there but not a spec."""
    m = BLOCK.search(body or "")
    if not m:
        return None
    try:
        spec = json.loads(m.group(1))
    except ValueError as e:
        raise ValueError(f"ga-act block is not JSON: {e}") from None
    if not isinstance(spec, dict) or not isinstance(spec.get("item"), dict):
        raise ValueError("ga-act block must be an object with an item")
    if "max_turns" in spec:
        mt = spec["max_turns"]
        if type(mt) is bool or not isinstance(mt, int) or not (1 <= mt <= 30):
            raise ValueError("max_turns must be an int 1..30")
    extra = set(spec) - SPEC_KEYS
    if extra:
        raise ValueError(f"ga-act block: unknown key(s) {', '.join(sorted(extra))}")
    return spec


def _git(repo: Path, *a: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=check)


def checkout_for(cfg: dict[str, Any], spec: dict[str, Any]) -> tuple[Path, str]:
    """(checkout, repo name for the report): the item's ``repo`` from act.repos, else act.repo."""
    act = cfg.get("act") or {}
    name = spec.get("repo")
    if name:
        entry = (act.get("repos") or {}).get(name)
        if not entry:
            raise ValueError(f"unknown item repo {name!r} (bridge config act.repos: {sorted(act.get('repos') or {})})")
        path, repo_name = (entry, "") if isinstance(entry, str) else (entry.get("path", ""), entry.get("repo_name", ""))
    else:
        path, repo_name = act.get("repo", ""), act.get("repo_name", "")
    checkout = Path(path or "").expanduser().resolve()
    if not path or not (checkout / ".git").exists():
        raise ValueError(f"act repo is not a git checkout: {checkout}")
    return checkout, repo_name or checkout.name


def _base(checkout: Path, spec: dict[str, Any]) -> str:
    base = spec.get("base")
    if not base:  # the remote's default branch
        head = _git(checkout, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).stdout.strip()
        base = head.rsplit("/", 1)[-1] if head else _git(checkout, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not BRANCH.match(base) or base.startswith("-"):
        raise ValueError(f"bad base branch name: {base!r}")
    return base


def prepare(spec: dict[str, Any], checkout: Path, did: str) -> Path:
    """The worktree for one item, from origin/<base>, with baseline's tests in it. ValueError on a bad spec."""
    for rel in spec.get("tests") or {}:
        if not SAFE_PATH.match(rel) or rel.split("/")[0] in (".git", ".ga") or rel.startswith(".env"):
            raise ValueError(f"bad test path: {rel}")
    owned = list(spec["item"].get("files") or [])
    for rel in spec.get("rewrite") or []:
        if not SAFE_PATH.match(rel) or rel not in owned:
            raise ValueError(f"bad rewrite path (must be one of the item's files): {rel}")
    base = _base(checkout, spec)
    _git(checkout, "fetch", "-q", "origin", base)
    wt = checkout.parent / f"{checkout.name}-agv-{did}"
    if wt.exists():
        _git(checkout, "worktree", "remove", "--force", str(wt), check=False)
    _git(checkout, "worktree", "add", "-q", "-B", f"agv/{did}", str(wt), f"origin/{base}")
    nm = checkout / "frontend" / "node_modules"
    if nm.is_dir() and (wt / "frontend").is_dir() and not (wt / "frontend" / "node_modules").exists():
        os.symlink(nm, wt / "frontend" / "node_modules")
    for rel, text in (spec.get("tests") or {}).items():
        p = wt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    for rel in spec.get("rewrite") or []:  # BD-450: a small model writes the whole file instead of exact edits
        p = wt / rel
        if p.is_file() and not p.is_symlink():
            p.unlink()
    return wt


def _expand(argv: list[str], subs: dict[str, str]) -> list[str]:
    out = []
    for x in argv:
        for k, v in subs.items():
            x = x.replace("{" + k + "}", v)
        out.append(x)
    return out


def act_argv(cfg: dict[str, Any], spec: dict[str, Any], wt: Path, tmp: Path, model: str) -> list[str]:
    act = cfg.get("act") or {}
    ladder = list(spec.get("ladder") or act.get("ladder") or [])
    max_turns = spec.get("max_turns", act.get("max_turns", 20))
    argv = [sys.executable, "-m", "ga", "act", "--item", str(tmp / "item.json"), "--repo", str(wt),
            "--backend", act.get("backend", "agv"), "--model", model,
            *(["--ladder", ",".join(ladder)] if ladder else []),
            "--options", json.dumps(act.get("options") or {}), "--config", str(tmp / "commands.json"),
            "--state", str(Path(act.get("state_dir") or "~/.ga/act-bridge").expanduser()),
            "--max-turns", str(int(max_turns))]
    return argv


def run_act(cfg: dict[str, Any], spec: dict[str, Any], wt: Path, checkout: Path, model: str) -> dict[str, Any]:
    """``ga act`` in the worktree; {code, out, result (act/1 dict or None)}."""
    act = cfg.get("act") or {}
    subs = {"venv_python": str(checkout / ".venv" / "bin" / "python"), "checkout": str(checkout),
            "python": sys.executable}
    cmds = dict(spec.get("commands") or {})
    cmds["commands"] = {k: _expand(v, subs) for k, v in (cmds.get("commands") or {}).items()}
    tmp = Path(tempfile.mkdtemp(prefix="ga-bridge-act-"))
    (tmp / "item.json").write_text(json.dumps(spec["item"], ensure_ascii=False), encoding="utf-8")
    (tmp / "commands.json").write_text(json.dumps(cmds, ensure_ascii=False), encoding="utf-8")
    src = str(Path(__file__).resolve().parents[2])  # this ga, also when it runs from a checkout
    env = dict(EV.child_env(), PYTHONPATH=os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH", "")])))
    try:
        p = subprocess.run(act_argv(cfg, spec, wt, tmp, model), capture_output=True, text=True, env=env,
                           timeout=int(act.get("timeout_s", 3600)))
        code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        code, out = 124, f"ga act timed out after {int(act.get('timeout_s', 3600))} s"
    result = None
    for line in reversed(out.strip().splitlines()):
        try:
            result = json.loads(line)
        except ValueError:
            continue
        if isinstance(result, dict):
            break
        result = None
    return {"code": code, "out": out, "result": result}


def finish(wt: Path, checkout: Path, did: str) -> str:
    """Commit the change on agv/<id> and return the patch ('' when nothing changed); the worktree is removed."""
    try:
        _git(wt, "add", "-A")
        _git(wt, "reset", "-q", "--", "frontend/node_modules", check=False)  # the link prepare() made (BD-427)
        if not _git(wt, "status", "--porcelain").stdout.strip():
            patch = ""
        else:
            _git(wt, "-c", "user.name=agv", "-c", "user.email=agv@localhost", "commit", "-q", "-m", f"{did}: agv ga act")
            patch = _git(wt, "format-patch", "-1", "--stdout").stdout
    finally:
        _git(checkout, "worktree", "remove", "--force", str(wt), check=False)
    if secrets_in(patch):
        return "(the patch was withheld: it looked like it held a secret)"
    return patch


def publish(cfg: dict[str, Any], checkout: Path, repo_name: str, head: dict[str, Any], patch: str) -> dict[str, str] | None:
    """BD-457: push the agv commit as agv/<id>-r<rev> (never with force); only a real patch that passed the secret check."""
    act = cfg.get("act") or {}
    if not act.get("push", True) or not patch.startswith("From ") or "withheld" in patch[:200]:
        return None
    did, rev = head["id"], int(head.get("rev", 1))
    sha = _git(checkout, "rev-parse", f"agv/{did}", check=False).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None
    branch = f"agv/{did}-r{rev}"
    if _git(checkout, "push", "-q", act.get("remote", "origin"), f"{sha}:refs/heads/{branch}", check=False).returncode:
        return None
    return {"repo": repo_name, "branch": branch, "sha": sha}


def served_model(res: dict[str, Any], configured: str) -> str:
    """The model the item actually ran on: the last rung of a routed / laddered run, else the configured one."""
    rungs = res.get("rungs") if isinstance(res.get("rungs"), list) else []
    last = rungs[-1].get("model") if rungs and isinstance(rungs[-1], dict) else None
    return str(last or configured or "?")


def report(cfg: dict[str, Any], head: dict[str, Any], run: dict[str, Any], patch: str, model: str,
           commit: dict[str, str] | None = None, pushed_wanted: bool = False) -> str:
    res = run.get("result") or {}
    ok = run["code"] == 0 and res.get("status") == "done"
    tok = res.get("tokens") or {}
    ev = [f"ga act exit {run['code']}, status {res.get('status', '?')}: {str(res.get('reason', ''))[:160]}",
          f"turns {res.get('turns', '?')}, changed {', '.join(res.get('changed') or []) or 'nothing'}",
          "self-reported through the agy bridge; baseline verifies the patch"]
    items = [{"id": d["id"], "state": "met" if ok else "unmet", "evidence": ev}
             for d in head.get("done_when") or [] if re.match(r"^D\d+$", str(d.get("id", "")))]
    rep: dict[str, Any] = {
        "schema": "report/2", "from": cfg["name"],
        "handled": [{"id": head["id"], "rev_seen": int(head.get("rev", 1)), "status": "done"}],
        "items": items or [{"id": "D1", "state": "met" if ok else "unmet", "evidence": ev}],
        "results": [{"name": "input_tokens", "value": int(tok.get("input") or 0)},
                    {"name": "tokens", "value": int(tok.get("total") or 0)},
                    {"name": "turns", "value": int(res.get("turns") or 0)},
                    {"name": "model", "value": served_model(res, model)}]}
    blockers = []
    if commit:
        rep["commits"] = [commit]
        rep["change_size"] = "implementation"
    elif pushed_wanted:
        blockers.append({"kind": "permission", "what": "the agv commit was not pushed (git push failed); the patch is below"})
    if not ok:
        blockers.append({"kind": "dependency", "what": f"ga act: {str(res.get('reason') or run['out'][-200:])}"[:300]})
    if blockers:
        rep["blockers"] = blockers
    tail = run["out"].strip()[-3000:]
    if secrets_in(tail):
        tail = "(withheld: looked like it held a secret)"
    body = "```ga\n" + json.dumps(rep, ensure_ascii=False, separators=(",", ":")) + "\n```\n\n## ga act result\n\n```text\n" + \
        tail.replace("```", "'''") + "\n```\n"
    if "trace" in res:
        lines = []
        for t in res["trace"]:
            acts = "; ".join(t.get("actions", []))
            s = f"t{t.get('turn')} card {t.get('card_tokens')} applied {t.get('applied')} rejected {t.get('rejected')}: {acts}"
            if t.get("dropped"):
                s += f" dropped {len(t['dropped'])}"
            lines.append(s)
        sec = "\n".join(lines)
        if len(sec) > 4000:
            sec = sec[:4000]
        body += "\n## turns\n\n```text\n" + sec.replace("```", "'''") + "\n```\n"
    if patch:
        cut = patch if len(patch) <= PATCH_CAP else patch[:PATCH_CAP] + "\n(patch cut at the cap)\n"
        body += "\n## patch\n\n```diff\n" + cut.replace("```", "'''") + "\n```\n"
    return body


def handle(cfg: dict[str, Any], head: dict[str, Any], spec: dict[str, Any], model: str | None = None,
           runner: Callable[..., dict] = run_act) -> tuple[str, bool]:
    """One code-work directive end to end: (report/2 text, ok)."""
    checkout, repo_name = checkout_for(cfg, spec)
    model = model or (cfg.get("act") or {}).get("model") or ""
    if not model and not spec.get("ladder") and not (cfg.get("act") or {}).get("ladder"):
        raise ValueError("no model for ga act: set act.model in the bridge config or model in the directive")
    wt = prepare(spec, checkout, head["id"])
    try:
        run = runner(cfg, spec, wt, checkout, model)
    finally:
        patch = finish(wt, checkout, head["id"])
    res = run.get("result") or {}
    ok = run["code"] == 0 and res.get("status") == "done"
    # only a met run is pushed (an unmet one's patch still rides in the report, for the hub to read)
    commit = publish(cfg, checkout, repo_name, head, patch) if ok else None
    wanted = ok and bool((cfg.get("act") or {}).get("push", True) and patch.startswith("From "))
    return report(cfg, head, run, patch, model, commit, wanted), ok
