"""G4 `ga rlo doctor` -- 실행 전 점검. 하나라도 어긋나면 실패다(닫는 쪽, GA_RLO.md §2).

    install.pins      두 SDK(와 rlo 의 일곱 의존)가 고정 sha 로 깔렸나(pip 의 direct_url.json 을 읽는다)
    install.contracts 모형이 action-model/1 로 읽히는 판인가 · Sensor(state-export)가 있나
    config            ga.json 이 있고 ga 의 검사를 지나나
    preset            작업 턴 기본값 그대로인가(G1: enforce · grant Bash 하나 · matcher * · 기록 · ga 가드 · 샌드박스 require)
    model             가드의 모형 파일이 action-model/1 로 읽히나
    guard.program     ga 가 턴을 열기 전에 보는 그대로, 가드 프로그램이 있고 실행할 수 있나
    replay            설정된 가드 명령 그대로, 기록된 transcript 를 재생한다(`--now-ms` = 마지막 시각 + 1 초).
                      정상 일은 막히지 않고, 모형에 없는 도구(WebFetch · Agent)는 A1 로 막히며, 어디에도 allow 가 없다
    permission        사람의 허락(decision/1, by user)이 이 Runner · 모형 · 샌드박스 · 예산을 덮나(ga 의 검사 그대로)
    sandbox           OS 쓰기 샌드박스가 여기서 되나
    person.settings   사람의 설정(~/.claude · CLAUDE_CONFIG_DIR)과 허브 세션의 프로젝트 설정에 rlo 가드가 없나(P4)

`--install-only` 는 설정 없이 설치만 본다(빈 디렉터리, 묶음 설치 검사): pins · contracts · 시작 모형 · 시작 프리셋 재생 · person.settings.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .. import _pins
from . import preset

# 막히면 안 되는 기록된 transcript(rlo 의 것, enforce 에서 {} 가 나와야 한다)
MUST_PASS = ("normal", "normal_no_current_use", "first_call", "first_call_no_current_use", "after_failure",
             "read_after_failure")
# 모형에 없는 도구: enforce 에서 A1 로 막혀야 한다(BD-163). 막히지 않으면 shadow 이거나 모형이 넓어진 것이다
MUST_DENY = (("WebFetch", {"url": "https://example.invalid/"}, "A1"), ("Agent", {"prompt": "p", "description": "d"}, "A1"))
TIMEOUT = 120


@dataclass
class Check:
    check: str
    ok: bool
    detail: str = ""


def installed_commit(dist: str) -> str | None:
    try:
        raw = metadata.distribution(dist).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    return (json.loads(raw).get("vcs_info") or {}).get("commit_id")


def pin_checks() -> list[Check]:
    out = []
    for name, pin in _pins.PINS.items():
        dist, _, _, sha = pin
        got = installed_commit(dist)
        out.append(Check(f"install.pins.{name}", got == sha,
                         f"{dist} {got[:7] if got else 'not from git'} (pinned {sha[:7]})"))
    try:
        import rlo

        v = rlo.versions()
    except Exception as e:  # noqa: BLE001
        return out + [Check("install.pins.rlo-deps", False, f"rlo.versions(): {type(e).__name__}")]
    want = {**v["pins"], **{k: s for g in v["extras"].values() for k, s in g.items()}}
    bad = sorted(k for k, s in want.items() if v["installed"].get(k) != s)
    out.append(Check("install.pins.rlo-deps", not bad, f"{len(want) - len(bad)}/{len(want)} at rlo's pins"
                     + (f"; off: {', '.join(bad)}" if bad else "")))
    c = v["contracts"]
    ok = c.get("action-model") == "action-model/1" and c.get("state-export") is not None
    out.append(Check("install.contracts", ok, f"action-model {c.get('action-model')} · state-export {c.get('state-export')}"))
    return out


def _clean_env(home: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
    env["HOME"] = home
    return env


def _call(argv: list[str], data: dict, env: dict[str, str], cwd: str) -> tuple[int, str]:
    p = subprocess.run(argv, input=json.dumps(data), capture_output=True, text=True, env=env, cwd=cwd, timeout=TIMEOUT)
    return p.returncode, p.stdout.strip()


def replay(guard: dict[str, Any], *, session: str = "doctor", where: str = "replay",
           extra_env: dict[str, str] | None = None) -> list[Check]:
    """설정된 가드 명령을 Claude Code 명령 훅처럼 부른다: 표준입력에 훅 입력 JSON, 표준출력의 JSON 하나.

    As a turn runs it (CMD-GR6 S4): a clean environment plus the Runner's ``extra_env``, and a working directory that
    is not the caller's -- from a source checkout, ``python -m ga.rlo.hook`` would otherwise import ga from the
    current directory and pass here while the real turn (in its worktree) cannot import it."""
    from rlo.example_hooks import hook_input, now_after

    out: list[Check] = []
    with tempfile.TemporaryDirectory(prefix="ga-rlo-doctor-") as home:
        cmd = guard["command"].replace("{session}", session).replace("{home}", home)
        try:
            base = shlex.split(cmd)
        except ValueError:
            return [Check(where, False, "guard command cannot be read")]
        env = dict(_clean_env(home), **(extra_env or {}))
        cases = [(n, hook_input(n), now_after(n), None) for n in MUST_PASS]
        for tool, args, rule in MUST_DENY:
            d = dict(hook_input("normal"), tool_name=tool, tool_input=args, tool_use_id=f"probe-{tool}")
            cases.append((f"probe {tool}", d, now_after("normal"), rule))
        for name, data, now, rule in cases:
            try:
                code, text = _call(base + ["--now-ms", repr(now)], data, env, cwd=home)
            except (OSError, subprocess.TimeoutExpired) as e:
                out.append(Check(f"{where}.{name}", False, type(e).__name__))
                continue
            try:
                res = json.loads(text) if text else {}
            except ValueError:
                out.append(Check(f"{where}.{name}", False, f"exit {code}, stdout is not JSON"))
                continue
            dec = ((res or {}).get("hookSpecificOutput") or {}).get("permissionDecision")
            if code != 0:
                out.append(Check(f"{where}.{name}", False, f"exit {code}: Claude Code would let the call through"))
            elif dec == "allow" or str((res or {}).get("decision", "")).lower() in ("allow", "approve"):
                out.append(Check(f"{where}.{name}", False, "the guard said allow (it must only deny or say nothing, P3)"))
            elif rule is None:
                reason = ((res or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")
                out.append(Check(f"{where}.{name}", dec is None, "not blocked" if dec is None else f"blocked: {reason[:80]}"))
            else:
                reason = ((res or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")
                ok = dec == "deny" and f"({rule})" in reason
                out.append(Check(f"{where}.{name}", ok, f"denied {rule}" if ok else
                                 f"not denied by {rule} ({dec or 'no decision'}): shadow mode or a wider model?"))
    return out


def person_settings(hub_dir: Path | None) -> list[Check]:
    """P4: 사람의 설정과 허브 세션의 설정에 rlo 가드(또는 ga_rlo 의 명령)가 없다."""
    paths = [Path.home() / ".claude" / "settings.json", Path.home() / ".claude" / "settings.local.json"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        paths.append(Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json")
    if hub_dir is not None:
        paths += [hub_dir / ".claude" / "settings.json", hub_dir / ".claude" / "settings.local.json"]
    found, unread = [], []
    for p in dict.fromkeys(paths):
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8") or "{}")
            groups = [g for v in (d.get("hooks") or {}).values() for g in v]
            cmds = [h.get("command", "") for g in groups for h in g.get("hooks", [])]
        except (ValueError, AttributeError, TypeError):
            unread.append(str(p))
            continue
        if any(preset.is_rlo(c) or "ga_rlo" in c or "ga rlo" in c for c in cmds):
            found.append(str(p))
    if unread:
        return [Check("person.settings", False, f"cannot read {', '.join(unread)}")]
    return [Check("person.settings", not found, f"rlo guard in {', '.join(found)} (P4: guards go only in a turn's own "
                  "settings)" if found else "no rlo guard in the person's or the hub session's settings")]


def install_checks() -> list[Check]:
    out = pin_checks()
    try:
        preset.load_model(preset.packaged_model())
        out.append(Check("model", True, "rlo cc_tools_model.json reads as action-model/1"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("model", False, f"rlo cc_tools_model.json: {type(e).__name__}"))
        return out
    with tempfile.TemporaryDirectory(prefix="ga-rlo-preset-") as d:
        model = preset.write_model(Path(d) / preset.MODEL_FILE)
        out += replay(preset.rlo_guard(model), where="replay.preset")
    return out


def config_checks(config: Path, ga_dir: str | None) -> list[Check]:
    from ga import config as gacfg
    from ga.__main__ import _hub

    out: list[Check] = []
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
        cfg = gacfg.from_dict(raw, config.parent)
    except Exception as e:  # noqa: BLE001
        return [Check("config", False, f"{config}: {e}"[:300])]
    out.append(Check("config", True, str(config)))
    probs = preset.problems(raw)
    out.append(Check("preset", not probs, "; ".join(probs) if probs else "enforce · grant Bash · matcher * · record · "
                     "ga guard on · sandbox require"))
    found = preset.rlo_guards(cfg.runner)
    for i, g, p in found:
        try:
            preset.load_model(p.model)
            out.append(Check(f"model[{i}]", True, f"{p.model} reads as action-model/1"))
        except Exception as e:  # noqa: BLE001
            out.append(Check(f"model[{i}]", False, f"{p.model}: {type(e).__name__}"))
    hub = _hub(SimpleNamespace(config=str(config), ga_dir=ga_dir))
    for s in cfg.sessions:
        gap = hub._guard_gap(s)
        out.append(Check(f"guard.program.{s}", not gap, gap or "guard programs are there"))
    for i, g, p in found:
        out += replay(g, where=f"replay[{i}]", extra_env=cfg.runner.get("extra_env"))
    gap = hub._permission_gap(hub.load_state())
    out.append(Check("permission", not gap, gap or f"{cfg.runner.get('permission')} covers this Runner"))
    from ga.adapters import sandbox

    out.append(Check("sandbox", sandbox.available(), "unprivileged user namespaces work here" if sandbox.available()
                     else "no OS write sandbox here: with sandbox require no turn runs"))
    return out


def run(config: str | Path = "ga.json", ga_dir: str | None = None, install_only: bool = False) -> tuple[bool, list[Check]]:
    config = Path(config).resolve()
    checks = install_checks() if install_only or not config.exists() else pin_checks()
    if not install_only:
        if config.exists():
            checks += config_checks(config, ga_dir)
        else:
            checks.append(Check("config", False, f"{config} not found (ga rlo init writes it)"))
    checks += person_settings(None if install_only else config.parent)
    return all(c.ok for c in checks), checks


def render(ok: bool, checks: list[Check], as_json: bool = False) -> str:
    if as_json:
        return json.dumps({"ok": ok, "checks": [asdict(c) for c in checks]}, ensure_ascii=False, indent=1)
    w = max(len(c.check) for c in checks) if checks else 0
    lines = [f"{'ok  ' if c.ok else 'FAIL'}  {c.check:{w}}  {c.detail}" for c in checks]
    lines.append("doctor: ok" if ok else f"doctor: FAIL ({sum(not c.ok for c in checks)} of {len(checks)})")
    return "\n".join(lines)


def run_remote(work_repo: str | Path = ".", venv: str | None = None) -> tuple[bool, list[Check]]:
    """Remote profile (CMD-GR2 S4): the work repo's guard files as generated, and replayed as Claude Code runs them."""
    import sys

    from . import remote

    repo = Path(work_repo).resolve()
    checks = pin_checks()
    probs = remote.problems(repo)
    checks.append(Check("remote.preset", not probs, "; ".join(probs) if probs else
                        "settings hooks · enforce · grants · no clock override · no git · plumbing · deny-report rule"))
    if not any(p.endswith(" is missing") for p in probs):  # no files, nothing to replay
        for name, ok, detail in remote.replay(repo, venv or sys.prefix):
            checks.append(Check(f"remote.replay.{name}", ok, detail))
    checks += person_settings(None)
    return all(c.ok for c in checks), checks
