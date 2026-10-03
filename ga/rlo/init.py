"""G3 `ga rlo init` -- ga 설정과 rlo 모형 파일을 한 번에 쓴다(GA_RLO.md §2).

쓰는 것
- `<dir>/ga.json` (ga-config/1): Runner(headless · agent_sdk) · 샌드박스 require · ga 가드 켬 · rlo 가드 프리셋(G1) · Judge ·
  예산 · 저장소 · 세션 · 소유표.
- `<dir>/.ga-rlo/cc_tools_model.json`: rlo 의 시작 모형 사본(이미 있으면 그대로 둔다).

하지 않는 것
- 허락(`ga permit`)을 만들지 않는다. 사람이 칠 명령을 **출력만** 한다(P3, METHOD §4c 5). 설정의 `runner.permission` 은 비워
  두고, 사람이 허락을 남긴 뒤 그 id 를 넣는다. 그 전에는 ga 가 게이트 6 으로 멈추고 `ga rlo doctor` 가 실패한다.
- 사람의 설정(`~/.claude`)과 허브 세션 설정을 건드리지 않는다(P4). 가드는 ga 가 턴마다 쓰는 전용 설정에만 들어간다.
- 있는 `ga.json` 을 덮지 않는다(`--force` 없이는).

프로필
- `local`(기본): 이 기계에서 ga 가 작업 턴을 연다(헤드리스 CLI 또는 Agent SDK).
- `remote`: 작업 세션이 원격 세션이고 rlo 가드를 작업 저장소의 프로젝트 설정으로 거는 경우(GA_RLO.md §7). 꼴은 CMD-GR2 에서
  정한다 -- 지금은 무엇도 쓰지 않고 멈춘다.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from . import preset

PROFILES = ("local", "remote")


class InitError(ValueError):
    pass


def parse_session(spec: str) -> tuple[str, dict[str, Any]]:
    """`이름:머리글자:브랜치:저장소[,저장소]`."""
    parts = spec.split(":")
    if len(parts) != 4 or not all(parts):
        raise InitError(f"--session {spec!r}: expected NAME:PREFIX:BRANCH:REPO[,REPO]")
    name, prefix, branch, repos = parts
    return name, {"prefix": prefix, "branch": branch, "repos": [r for r in repos.split(",") if r]}


def parse_repo(spec: str) -> tuple[str, str]:
    name, eq, path = spec.partition("=")
    if not eq or not name or not path:
        raise InitError(f"--repo {spec!r}: expected NAME=PATH")
    return name, path


def build(*, base: Path, hub: str, repos: dict[str, str], sessions: dict[str, dict[str, Any]], integration_branch: str,
          runner: str = "headless", model: str | None = None, budget: dict[str, float] | None = None,
          max_budget_usd: float | None = 0.5, judge: str = "file", python: str | None = None,
          guidance: str | None = None, session_guidance: str | None = None) -> dict[str, Any]:
    """ga-config/1 원본 dict. 모형 경로는 `<base>/.ga-rlo/cc_tools_model.json`."""
    if runner not in ("headless", "agent_sdk"):
        raise InitError(f"--runner {runner!r}: headless or agent_sdk (only these write a turn's own settings)")
    owned: set[str] = set()
    ownership = []
    for s, d in sessions.items():
        for r in d["repos"]:
            if r not in repos:
                raise InitError(f"session {s}: unknown repo {r}")
            if r not in owned:  # 한 파일에 주인 하나: 저장소를 처음 적은 세션이 갖는다
                ownership.append({"repo": r, "path": "*", "session": s})
                owned.add(r)
    run: dict[str, Any] = {"kind": runner, "sandbox": "require", "guard": True,
                           "guards": [preset.rlo_guard(base / preset.STATE_DIR / preset.MODEL_FILE, python=python)]}
    if model:
        run["model"] = model
    if max_budget_usd is not None:
        run["max_budget_usd"] = max_budget_usd
    raw = {
        "schema": "ga-config/1",
        "hub": {"name": hub, **({"guidance": guidance} if guidance else {}),
                **({"session_guidance": session_guidance} if session_guidance else {})},
        "integration_branch": integration_branch,
        "repos": {n: {"path": p} for n, p in repos.items()},
        "sessions": sessions,
        "ownership": ownership,
        "budget": dict(budget or {"runs": 6}),
        "runner": run,
        "judge": {"kind": judge},
    }
    from ga import config as gacfg

    gacfg.from_dict(raw, base)  # ga 의 검사를 그대로 받는다(틀리면 FormError)
    left = preset.problems(raw) if (base / preset.STATE_DIR / preset.MODEL_FILE).exists() else None
    if left:
        raise InitError("preset problems: " + "; ".join(left))
    return raw


def permit_command(raw: dict[str, Any]) -> str:
    """사람이 칠 허락 명령(출력만 한다)."""
    r = raw["runner"]
    argv = ["ga", "permit", "--runner", r["kind"]]
    if r.get("model"):
        argv += ["--model", r["model"]]
    argv += ["--sandbox", r.get("sandbox", "auto")]
    for k, v in (raw.get("budget") or {}).items():
        argv += ["--budget", f"{k}={v:g}"]
    if raw.get("judge", {}).get("kind") == "llm":
        argv += ["--measurement-calls"]
    return " ".join(shlex.quote(x) for x in argv) + " --note '<사람이 직접 한 답의 요지>'"


def run(*, directory: str | Path, profile: str = "local", force: bool = False, **kw: Any) -> dict[str, Any]:
    """설정과 모형을 쓰고 {config, model, permit} 를 낸다."""
    if profile not in PROFILES:
        raise InitError(f"--profile {profile!r}: one of {PROFILES}")
    if profile == "remote":
        return run_remote(work_repo=kw.get("work_repo"), name=kw.get("name") or "worker", force=force)
    kw.pop("work_repo", None)
    kw.pop("name", None)
    base = Path(directory).resolve()
    cfg_path = base / "ga.json"
    if cfg_path.exists() and not force:
        raise InitError(f"{cfg_path} exists: not overwritten (--force to replace it)")
    model_path = base / preset.STATE_DIR / preset.MODEL_FILE
    raw = build(base=base, **kw)  # 먼저 검사한다: 틀리면 아무것도 쓰지 않는다
    preset.write_model(model_path)
    left = preset.problems(raw)
    if left:
        raise InitError("preset problems: " + "; ".join(left))
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"config": str(cfg_path), "model": str(model_path), "permit": permit_command(raw)}


def run_remote(*, work_repo: str | Path | None, name: str = "worker", force: bool = False) -> dict[str, Any]:
    """Remote profile (CMD-GR2): the guard as project settings in the work repo. Writes files, never runs git (S5)."""
    from . import remote

    if not work_repo:
        raise InitError("--profile remote needs --work-repo PATH (a checkout of the worker's repository)")
    try:
        out = remote.write(work_repo, name=name, force=force)
    except (OSError, ValueError) as e:
        raise InitError(str(e)) from e
    return dict(out, profile="remote")
