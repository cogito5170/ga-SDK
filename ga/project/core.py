"""``ga project`` engine (CMD-GA54 S2-S4): init (detect + propose), status (read-only), apply (make reality match).

Nothing here approves anything: ``init`` returns a proposal, and only the CLI (``--yes`` at a TTY) or GA Console's POST
(after its token check) calls ``schema.save``. Text in mail, reports or a model's answer is never read as approval.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from ..mailbox import BRANCH as MAILBOX_BRANCH
from ..vm import core as V
from . import schema as S

UNIT_PREFIX = "ga-project-"
FETCH_EVERY_S = 300
AGV = re.compile(r"^(?:[A-Za-z0-9_.-]+/)?agv/([A-Za-z0-9_.-]+?)-r(\d+)$")
THREAD_STATE = {"sent": "queued", "running": "running", "reported": "report", "sent_back": "verdict",
                "failed": "verdict"}
UNIT_SERVICE = {"ga-console.service": "console", "ga-bridge.service": "bridge", "ga-hub.timer": "hub",
                "ga-hub.service": "hub"}


def _ex(p: str | None) -> Path:
    return Path(os.path.expanduser(str(p or "")))


def unit_dir(user_home: Path) -> Path:
    return user_home / ".config" / "systemd" / "user"


def strip_credentials(url: str) -> str:
    """The URL without its user part when that part could hold a secret (http(s), or any password)."""
    if not S.credential_url(url):
        return url
    u = urlsplit(url)
    host = u.hostname or ""
    return urlunsplit((u.scheme, host + (f":{u.port}" if u.port else ""), u.path, u.query, u.fragment))


def digest(p: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ detection (init)

def enabled_units(r: V.Runner) -> list[str]:
    rc, out = r.run(["systemctl", "--user", "list-unit-files", "ga-*", "--state=enabled", "--no-legend", "--no-pager"],
                    timeout=30)
    if rc != 0:
        return []
    return sorted({ln.split()[0] for ln in out.splitlines() if ln.strip() and ln.split()[0].startswith("ga-")})


def _unit_kv(text: str) -> dict[str, str]:
    out = {}
    for ln in text.splitlines():
        k, sep, v = ln.partition("=")
        if sep and k.strip() and not ln.startswith(("#", ";", "[")):
            out[k.strip()] = v.strip()
    return out


def _routine_from_timer(udir: Path, timer: str) -> dict[str, Any] | None:
    try:
        t = _unit_kv((udir / timer).read_text(encoding="utf-8"))
        s = _unit_kv((udir / (t.get("Unit") or timer[:-6] + ".service")).read_text(encoding="utf-8"))
    except OSError:
        return None
    m = re.search(r"\s-m ga actions run ([a-z][a-z0-9_-]{0,31})$", s.get("ExecStart", ""))
    every = t.get("OnCalendar") or t.get("OnUnitActiveSec")
    if not m or not every:
        return None
    rname = re.sub(r"^ga-project-[a-z0-9_.-]+?--", "", timer[:-6])
    return {"name": rname, "every": every, "action": m.group(1)}


def _git1(r: V.Runner, repo: Path, *a: str) -> str:
    rc, out = V._git(r, repo, *a)
    return out.strip() if rc == 0 else ""


def _console_cfg(h: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads((h / "console.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def detect(*, name: str | None = None, session_file: str | None = None, h: Path | None = None,
           user_home: Path | None = None, runner: V.Runner | None = None,
           console: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """What already exists, as a project: (project, source per field, notes). Reads only local state."""
    h, user_home, r = h or S.home(), user_home or Path.home(), runner or V.Runner()
    cfg = console if console is not None else _console_cfg(h)
    p: dict[str, Any] = {"schema": S.SCHEMA}
    src: dict[str, str] = {}
    notes: list[str] = []
    cname = "console config" if console is not None else f"console config ({h / 'console.json'})"
    # repos: the console's checkouts, their remotes and branches
    repos = []
    for c in (cfg or {}).get("repos") or []:
        if not isinstance(c, dict) or not c.get("name") or not c.get("path"):
            continue
        path, rn = str(c["path"]), str(c["name"]).lower()
        if not S.NAME.match(rn):
            notes.append(f"repo {c['name']!r}: name not usable, left out")
            continue
        rec: dict[str, Any] = {"name": rn, "path": path}
        where = [f"from {cname}"]
        url = _git1(r, _ex(path), "remote", "get-url", "origin") if _ex(path).is_dir() else ""
        if url:
            clean = strip_credentials(url)
            rec["url"] = clean
            where.append("url from git remote" + (" (credentials removed)" if clean != url else ""))
        br = c.get("integration_branch")
        if not br and _ex(path).is_dir():
            br = _git1(r, _ex(path), "rev-parse", "--abbrev-ref", "HEAD")
            if br:
                where.append("branch from git HEAD")
        if br:
            rec["branch"] = br
        repos.append(rec)
        src[f"repos.{rn}"] = ", ".join(where)
    if repos:
        p["repos"] = repos
    # environment: enabled ga units first, then the console config, then the running python
    units = enabled_units(r)
    env: dict[str, Any] = {}
    vm_mark = next((u for u in ("ga-update.timer", "ga-console.service", "ga-hub.timer") if u in units), None)
    env["kind"] = "vm" if vm_mark else "local"
    src["environment.kind"] = (f"from {'timer' if vm_mark.endswith('.timer') else 'unit'} {vm_mark}" if vm_mark
                               else "no ga systemd unit enabled")
    services: list[str] = []
    why: list[str] = []
    for u in units:
        s = UNIT_SERVICE.get(u)
        if s and s not in services:
            services.append(s)
            why.append(f"{s} from {'timer' if u.endswith('.timer') else 'unit'} {u}")
    if cfg is not None:
        for s, ok in (("console", True), ("bridge", bool(cfg.get("bridge"))),
                      ("token-api", "token-api" in (cfg.get("services") or {})),
                      ("token-web", "token-web" in (cfg.get("services") or {}))):
            if ok and s not in services:
                services.append(s)
                why.append(f"{s} from {cname}")
    env["services"] = [s for s in S.SERVICES if s in services]
    src["environment.services"] = "; ".join(why) or "none found"
    vpy = user_home / "ga-venv" / "bin" / "python"
    if env["kind"] == "vm" and vpy.exists():
        env["python"], env["venv"] = str(vpy), str(user_home / "ga-venv")
        src["environment.python"] = "from ~/ga-venv (ga vm install)"
    else:
        env["python"] = sys.executable
        src["environment.python"] = "from the running python"
        if sys.prefix != sys.base_prefix:
            env["venv"] = sys.prefix
    p["environment"] = env
    # instructions: only the file a person names
    if session_file:
        f = _ex(session_file)
        if not f.is_file():
            notes.append(f"{session_file}: not a file, instructions left out")
        elif f.stat().st_size > S.INSTRUCTIONS_MAX:
            notes.append(f"{session_file}: over {S.INSTRUCTIONS_MAX} bytes, instructions left out")
        else:
            p["instructions"] = {"file": str(f)}
            src["instructions"] = "from --from-session-file"
    # routines: ga-project timers already enabled
    routines = []
    for u in units:
        if u.startswith(UNIT_PREFIX) and u.endswith(".timer"):
            rt = _routine_from_timer(unit_dir(user_home), u)
            if rt and S.NAME.match(rt["name"]):
                routines.append(rt)
                src[f"routines.{rt['name']}"] = f"from timer {u}"
    if routines:
        p["routines"] = routines
    # mailbox, models
    mb = (cfg or {}).get("mailbox")
    if isinstance(mb, dict) and mb.get("repo"):
        p["mailbox"] = {"repo": str(mb["repo"]), "branch": MAILBOX_BRANCH}
        src["mailbox"] = f"from {cname}"
    if (h / "hub.json").is_file():
        p["models"] = {"ladder": str(h / "hub.json")}
        src["models"] = f"from {h / 'hub.json'}"
    # name
    if name:
        p["name"], src["name"] = name, "from --name"
    else:
        p["name"] = repos[0]["name"] if repos else "ga"
        src["name"] = f"from {cname} (first repo)" if repos else "default"
    return p, src, notes


def merge(old: dict[str, Any], new: dict[str, Any], src: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    """``old`` wins for every field it sets (source 'kept'); repos and routines are merged by name."""
    out, s = dict(new), dict(src)
    for k, v in old.items():
        if k in ("repos", "routines") and isinstance(v, list) and isinstance(new.get(k), list):
            have = {x.get("name") for x in v if isinstance(x, dict)}
            out[k] = list(v) + [x for x in new[k] if x.get("name") not in have]
            for x in v:
                if isinstance(x, dict):
                    s[f"{k}.{x.get('name')}"] = "kept"
        else:
            out[k] = v
            for key in [key for key in s if key == k or key.startswith(k + ".")]:
                s[key] = "kept"
            s[k] = "kept"
    return out, s


def proposal(*, name: str | None = None, session_file: str | None = None, h: Path | None = None,
             user_home: Path | None = None, runner: V.Runner | None = None,
             console: dict[str, Any] | None = None) -> dict[str, Any]:
    """The init proposal: {project, sources, notes, problems, existing, diff, sha256}. Writes nothing."""
    h = h or S.home()
    if not name:
        have = S.names(h)
        name = have[0] if len(have) == 1 else None
    p, src, notes = detect(name=name, session_file=session_file, h=h, user_home=user_home, runner=runner,
                           console=console)
    old = S.load(p["name"], h)
    diff = ""
    if old is not None:
        p, src = merge(old, p, src)
        a = json.dumps(old, indent=1, ensure_ascii=False, sort_keys=True).splitlines()
        b = json.dumps(p, indent=1, ensure_ascii=False, sort_keys=True).splitlines()
        diff = "\n".join(difflib.unified_diff(a, b, "saved", "proposed", lineterm="", n=1))
    return {"project": p, "sources": src, "notes": notes, "problems": S.validate(p), "existing": old is not None,
            "diff": diff, "sha256": digest(p), "path": str(S.path_of(p["name"], h))}


def render_proposal(prop: dict[str, Any]) -> str:
    p, src = prop["project"], prop["sources"]
    lines = [f"proposed project {p['name']}  ({'update of ' if prop['existing'] else 'new: '}{prop['path']})"]
    for k in S.TOP[1:]:
        if k not in p:
            continue
        v = p[k]
        if isinstance(v, list):
            lines.append(f"  {k}:")
            for x in v:
                lines.append(f"    - {json.dumps(x, ensure_ascii=False)}  [{src.get(k + '.' + str(x.get('name')), '?')}]")
        elif isinstance(v, dict):
            subs = [x for x in src if x.startswith(k + ".")]
            where = src.get(k) or "; ".join(f"{x.split('.', 1)[1]} {src[x]}" for x in subs) or "?"
            lines.append(f"  {k}: {json.dumps(v, ensure_ascii=False)}  [{where}]")
        else:
            lines.append(f"  {k}: {v}  [{src.get(k, '?')}]")
    for n in prop["notes"]:
        lines.append(f"  note: {n}")
    for b in prop["problems"]:
        lines.append(f"  problem: {b}")
    if prop["diff"]:
        lines.append("diff against the saved project (what is set is kept):")
        lines.append(prop["diff"])
    elif prop["existing"]:
        lines.append("no change against the saved project")
    return "\n".join(lines)


def approve(prop: dict[str, Any], h: Path | None = None) -> Path:
    """Write the proposal. Called only by a human act: the CLI's --yes at a TTY, or the console's POST + token."""
    return S.save(prop["project"], h)


# ------------------------------------------------------------------ status (read-only)

def _timer_times(r: V.Runner, unit: str) -> dict[str, str]:
    rc, out = r.run(["systemctl", "--user", "show", unit, "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime",
                     "-p", "UnitFileState", "--no-pager"], timeout=30)
    kv = _unit_kv(out) if rc == 0 else {}
    return {"last": kv.get("LastTriggerUSec") or "never", "next": kv.get("NextElapseUSecRealtime") or "",
            "enabled": kv.get("UnitFileState") == "enabled"}


def routine_unit(project: str, routine: str) -> str:
    return f"{UNIT_PREFIX}{project}--{routine}"


def threads(p: dict[str, Any], repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from ..console import collectors as C
    base = next((r["path"] for r in p.get("repos") or [] if r.get("name") == "baseline"), None) \
        or (p.get("mailbox") or {}).get("repo")
    rows = []
    known: dict[str, str] = {}
    if base and _ex(base).is_dir():
        for w in C.work({"baseline": str(_ex(base))}):
            st = THREAD_STATE.get(w.get("status"))
            known[str(w.get("id"))] = st or ""
            if st:
                rows.append({"kind": w.get("kind"), "id": w.get("id"), "state": st, "title": w.get("title")})
    for rp in repos:
        for b in rp.get("agv", []):
            m = AGV.match(b)
            wid = m.group(1)
            st = known.get(wid) or known.get(f"CMD-{wid}") or "running"
            rows.append({"kind": "branch", "id": wid, "round": int(m.group(2)), "repo": rp["name"], "branch": b,
                         "state": st})
    return rows


def status(p: dict[str, Any], *, runner: V.Runner | None = None, fetch: bool = False, h: Path | None = None,
           clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Repo heads, threads by state, routines with last/next run. No network unless ``fetch`` (throttled)."""
    r, h = runner or V.Runner(), h or S.home()
    fetched = None
    if fetch:
        stamp = h / "projects" / f".{p['name']}.fetched"
        if stamp.exists() and clock() - stamp.stat().st_mtime < FETCH_EVERY_S:
            fetched = f"skipped: fetched under {FETCH_EVERY_S} s ago"
        else:
            fetched = "fetched"
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.touch()
    repos = []
    for rp in p.get("repos") or []:
        path = _ex(rp.get("path"))
        row: dict[str, Any] = {"name": rp.get("name"), "path": str(path), "branch": rp.get("branch"),
                               "present": (path / ".git").exists()}
        if row["present"]:
            if fetched == "fetched" and rp.get("branch"):
                V._git(r, path, "fetch", "origin", rp["branch"])
            row["head"] = _git1(r, path, "rev-parse", "HEAD")
            row["on"] = _git1(r, path, "rev-parse", "--abbrev-ref", "HEAD")
            row["dirty"] = bool(_git1(r, path, "status", "--porcelain"))
            refs = _git1(r, path, "for-each-ref", "--format=%(refname:short)", "refs/heads/agv", "refs/remotes/origin/agv")
            row["agv"] = sorted({b for b in refs.splitlines() if AGV.match(b)})
        repos.append(row)
    routines = []
    for rt in p.get("routines") or []:
        unit = routine_unit(p["name"], rt.get("name", ""))
        routines.append({**rt, "unit": unit + ".timer", **_timer_times(r, unit + ".timer")})
    ins = p.get("instructions") or {}
    excerpt = ins.get("text") or ""
    if ins.get("file"):
        try:
            excerpt = _ex(ins["file"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            excerpt = f"(cannot read {ins['file']})"
    return {"name": p["name"], "repos": repos, "threads": threads(p, repos), "routines": routines,
            "environment": p.get("environment") or {}, "instructions_excerpt": excerpt[:400], "fetch": fetched}


# ------------------------------------------------------------------ apply

def routine_files(p: dict[str, Any], rt: dict[str, Any], *, user_home: Path, h: Path) -> dict[str, str]:
    env = p.get("environment") or {}
    py = env.get("python") or (f"{env['venv']}/bin/python" if env.get("venv") else sys.executable)
    py = os.path.expanduser(py)
    wd = next((str(_ex(r["path"])) for r in p.get("repos") or [] if r.get("path")), str(user_home))
    unit = routine_unit(p["name"], rt["name"])
    svc = (f"[Unit]\nDescription=GA project {p['name']}: routine {rt['name']} (GA Action {rt['action']})\n\n"
           f"[Service]\nType=oneshot\nWorkingDirectory={wd}\nEnvironment=GA_HOME={h}\n"
           f"ExecStart={py} -m ga actions run {rt['action']}\nNoNewPrivileges=yes\n")
    every = rt["every"]
    when = (f"OnBootSec=2min\nOnUnitActiveSec={every}\n" if S.INTERVAL.match(every)
            else f"OnCalendar={every}\nPersistent=true\n")
    timer = (f"[Unit]\nDescription=GA project {p['name']}: routine {rt['name']} every {every}\n\n[Timer]\n{when}"
             f"Unit={unit}.service\n\n[Install]\nWantedBy=timers.target\n")
    return {f"{unit}.service": svc, f"{unit}.timer": timer}


def _ff(r: V.Runner, path: Path, branch: str, say: Callable[[str], None]) -> None:
    rc, out = V._git(r, path, "fetch", "origin", branch)
    if rc != 0:
        say(f"skip {path}: git fetch failed: {out.strip()[-120:]}")
        return
    on = _git1(r, path, "rev-parse", "--abbrev-ref", "HEAD")
    if on != branch:
        say(f"skip {path}: on {on or '?'}, not {branch} (not switched)")
        return
    before = _git1(r, path, "rev-parse", "HEAD")
    rc, out = V._git(r, path, "merge", "--ff-only", f"origin/{branch}")
    if rc != 0:
        say(f"skip {path}: {branch} is not a fast-forward of origin/{branch} (nothing forced)")
        return
    after = _git1(r, path, "rev-parse", "HEAD")
    say(f"fast-forwarded {path} {before[:7]} -> {after[:7]}" if before != after else f"{path} up to date")


def apply(p: dict[str, Any], *, yes: bool, runner: V.Runner | None = None, user_home: Path | None = None,
          h: Path | None = None, say: Callable[[str], None] = print) -> int:
    """Make reality match ``p``. Without ``yes`` only lists the steps. Never sudo, never reset or force, never reads a
    credential. 0 done (or nothing to do), 1 the project is invalid."""
    r, user_home, h = runner or V.Runner(), user_home or Path.home(), h or S.home()
    bad = S.validate(p)
    if bad:
        for b in bad:
            say(f"invalid: {b}")
        return 1
    tag = "" if yes else "would "
    for rp in p.get("repos") or []:
        path, branch, url = _ex(rp["path"]), rp["branch"], rp.get("url")
        try:
            how = V.plan_repo(r, path)
        except V.VmError as e:
            say(f"skip: {e}")
            continue
        if how == "clone":
            if not url:
                say(f"skip {path}: missing and no url to clone from")
                continue
            say(f"{tag}clone {url} ({branch}) -> {path}")
            if yes:
                path.parent.mkdir(parents=True, exist_ok=True)
                rc, out = r.run(["git", "clone", "--branch", branch, url, str(path)], env=V.GIT_ENV)
                if rc != 0:
                    say(f"skip {path}: git clone failed: {out.strip()[-120:]}")
        elif not yes:
            say(f"would fetch origin {branch} and fast-forward {path} (ff-only)")
        else:
            _ff(r, path, branch, say)
    udir = unit_dir(user_home)
    changed, timers = False, []
    for rt in p.get("routines") or []:
        for fname, text in routine_files(p, rt, user_home=user_home, h=h).items():
            f = udir / fname
            if f.is_file() and f.read_text(encoding="utf-8") == text:
                continue
            if yes:
                changed |= V._write(f, text, lambda m: say(m))
            else:
                say(f"would write {f}")
        timers.append(routine_unit(p["name"], rt["name"]) + ".timer")
    if yes and changed:
        say("systemctl --user daemon-reload")
        r.run(["systemctl", "--user", "daemon-reload"], timeout=60)
    for t in timers:
        rc, _ = r.run(["systemctl", "--user", "is-enabled", t], timeout=30)
        if rc == 0 and not changed:
            continue
        say(f"{tag}systemctl --user enable --now {t}")
        if yes:
            rc, out = r.run(["systemctl", "--user", "enable", "--now", t], timeout=60)
            if rc != 0:
                say(f"skip {t}: enable failed: {out.strip()[-120:]}")
    if not yes:
        say("dry run: nothing changed (ga project apply --yes at a terminal applies it)")
    return 0
