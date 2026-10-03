"""`ga rlo ...` -- rlo's commands inside ga (moved from ga_rlo 0.4.0 `ga-rlo`, CMD-GR5, GA_UNIFIED U2).

    ga rlo init     --hub H --repo N=PATH … --session N:P:BRANCH:REPOS …   G3: ga.json + rlo model (prints the permit only)
    ga rlo init     --profile remote --work-repo PATH [--name W]           GR2: the guard as a remote worker's project settings
    ga rlo doctor   [--install-only] [--profile remote --work-repo PATH] [--json]   G4: checks before running (exit 1 if off)
    ga rlo evidence [--json]                                               G2: per turn, ga's guard evidence
    ga rlo preset   [--model PATH]                                         G1: one runner.guards entry (JSON)
    ga rlo upgrade-remote [--work-repo PATH]                               prints how to move a remote guard to the pinned rlo
    ga rlo hooks …                                                         python -m rlo.hooks … as is; install-hook and
                                                                           uninstall-hook need --settings and refuse the
                                                                           person's settings (P4)

Common: --config PATH (default ga.json) · --ga-dir PATH (default <config dir>/.ga), given after `rlo`. ga's own
commands (tick, send, permit, ...) are `ga <command>`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

GA_COMMANDS = ("tick", "setup", "sandbox", "post", "send", "review", "permit", "answer", "prompt", "check", "render")  # ga's own
OWN = ("init", "doctor", "evidence", "preset", "upgrade-remote")
GLOBAL = ("--config", "--ga-dir")


def _split(argv: list[str]) -> tuple[dict[str, str], str | None, int]:
    """(전역 옵션, 하위 명령, 그 자리)."""
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in GLOBAL and i + 1 < len(argv):
            opts[a] = argv[i + 1]
            i += 2
            continue
        if any(a.startswith(g + "=") for g in GLOBAL):
            k, _, v = a.partition("=")
            opts[k] = v
            i += 1
            continue
        if a.startswith("-"):
            return opts, None, i
        return opts, a, i
    return opts, None, i


def person_settings_paths() -> list[Path]:
    paths = [Path.home() / ".claude" / "settings.json", Path.home() / ".claude" / "settings.local.json"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        paths.append(Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json")
    return [p.expanduser().resolve() for p in paths]


def hooks(rest: list[str]) -> int:
    from rlo.hooks import main as rlo_main

    if rest and rest[0] in ("install-hook", "uninstall-hook"):
        ap = argparse.ArgumentParser(add_help=False)
        ap.add_argument("--settings")
        a, _ = ap.parse_known_args(rest[1:])
        if not a.settings:
            print(f"ga rlo hooks {rest[0]}: --settings is required (ga rlo never installs into the person's settings, P4)",
                  file=sys.stderr)
            return 2
        if Path(a.settings).expanduser().resolve() in person_settings_paths():
            print(f"ga rlo hooks {rest[0]}: {a.settings} is the person's settings -- refused (P4: guards go only in a "
                  "work turn's own settings)", file=sys.stderr)
            return 2
    return rlo_main(rest)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ga rlo", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="ga-SDK 와 rlo-SDK 를 한 입구로: ga 가 일을 굴리고 기록 · 판정하고, rlo 가 그 안의 모형 턴을 지킨다.",
        epilog="rlo hooks (as is):  ga rlo hooks …  (python -m rlo.hooks)\n"
               "ga's own commands:  ga " + " · ".join(GA_COMMANDS) + "\n"
               "The permit (ga permit) is the person's. ga rlo init only prints it.")
    ap.add_argument("--config", default="ga.json")
    ap.add_argument("--ga-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", metavar="{init,doctor,evidence,preset,upgrade-remote,hooks}")
    p = sub.add_parser("init", help="G3: ga.json 과 rlo 모형을 쓴다(허락은 출력만)")
    p.add_argument("--dir", default=None, help="설정을 쓸 디렉터리(기본: --config 의 디렉터리)")
    p.add_argument("--profile", default="local", choices=("local", "remote"))
    p.add_argument("--hub", default="hub")
    p.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="local profile (one or more)")
    p.add_argument("--session", action="append", default=[], metavar="NAME:PREFIX:BRANCH:REPO[,REPO]",
                   help="local profile (one or more)")
    p.add_argument("--work-repo", default=None, help="remote profile: the worker's repository checkout")
    p.add_argument("--name", default="worker", help="remote profile: the worker session's name (record file name)")
    p.add_argument("--integration-branch", default="integration")
    p.add_argument("--runner", default="headless", choices=("headless", "agent_sdk"))
    p.add_argument("--model", default=None)
    p.add_argument("--budget", action="append", default=[], metavar="NAME=NUMBER", help="기본 runs=6")
    p.add_argument("--max-budget-usd", type=float, default=0.5, help="턴 하나의 비용 상한")
    p.add_argument("--judge", default="file", choices=("file", "llm"))
    p.add_argument("--guidance", default=None, help="허브 안내 파일(설정 디렉터리 기준, ga prompt --hub)")
    p.add_argument("--session-guidance", default=None, help="작업 세션 안내 파일(ga prompt <세션>)")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("doctor", help="G4: 실행 전 점검(어긋나면 exit 1)")
    p.add_argument("--install-only", action="store_true", help="설정 없이 설치만 본다")
    p.add_argument("--profile", default="local", choices=("local", "remote"))
    p.add_argument("--work-repo", default=".", help="remote profile: the worker's repository checkout")
    p.add_argument("--venv", default=None, help="remote profile: a venv with rlo installed (default: this one)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("evidence", help="G2: 턴마다 rlo 의 수와 라벨")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("preset", help="G1: runner.guards 항목 하나")
    p.add_argument("--model", default=None, help="모형 경로(기본: <설정 디렉터리>/.ga-rlo/cc_tools_model.json)")
    p = sub.add_parser("upgrade-remote", help="print the commands that move a remote guard's install.sh to the pinned rlo")
    p.add_argument("--work-repo", default=".")
    p.add_argument("--install-sh", default="ops/rlo/install.sh", help="path inside the work repo")
    sub.add_parser("hooks", help="python -m rlo.hooks … 그대로", add_help=False)
    return ap


def cmd_init(a) -> int:
    from . import init

    if a.profile == "remote":
        try:
            out = init.run(directory=".", profile="remote", force=a.force, work_repo=a.work_repo, name=a.name)
        except init.InitError as e:
            print(f"ga rlo init: {e}", file=sys.stderr)
            return 2
        for rel in out["written"]:
            print(f"wrote {Path(out['repo']) / rel}")
        print("\nga rlo does not commit or push guard files (an AI may not change another session's guard, BD-196).")
        print("A human reviews them, then runs:")
        for c in out["commands"]:
            print(f"  {c}")
        print("Give the hub ownership of these paths in its ga.json:")
        for row in out["ownership"]:
            print(f"  {json.dumps(row)}")
        print("Then: ga rlo doctor --profile remote --work-repo <checkout>")
        return 0
    if not a.repo or not a.session:
        print("ga rlo init: the local profile needs --repo and --session", file=sys.stderr)
        return 2
    try:
        repos = dict(init.parse_repo(r) for r in a.repo)
        sessions = dict(init.parse_session(s) for s in a.session)
        budget = {k: float(v) if "." in v else int(v) for k, v in (x.split("=", 1) for x in a.budget)} or None
        out = init.run(directory=a.dir or Path(a.config).resolve().parent, profile=a.profile, force=a.force, hub=a.hub,
                       repos=repos, sessions=sessions, integration_branch=a.integration_branch, runner=a.runner,
                       model=a.model, budget=budget, max_budget_usd=a.max_budget_usd, judge=a.judge,
                       guidance=a.guidance, session_guidance=a.session_guidance)
    except (init.InitError, ValueError) as e:
        print(f"ga rlo init: {e}", file=sys.stderr)
        return 2
    print(f"wrote {out['config']}")
    print(f"wrote {out['model']}")
    print("\n다음은 사람이 한다(ga rlo 는 허락을 만들지 않는다, METHOD §4c 5):")
    print(f"  1. {out['permit']}")
    print('  2. 나온 BD-n 을 ga.json 의 "runner": {..., "permission": "BD-n"} 에 넣는다')
    print("  3. ga rlo doctor")
    return 0


def cmd_doctor(a) -> int:
    from . import doctor

    if a.profile == "remote":
        ok, checks = doctor.run_remote(a.work_repo, a.venv)
    else:
        ok, checks = doctor.run(a.config, a.ga_dir, install_only=a.install_only)
    print(doctor.render(ok, checks, a.json))
    return 0 if ok else 1


def evidence_rows(config: str, ga_dir: str | None) -> list[dict]:
    """Per turn: ga's own guard evidence (counts, deny labels, Sensor state). Old runs: the retired side file."""
    from .bridge import read_evidence

    state = (Path(ga_dir) if ga_dir else Path(config).resolve().parent / ".ga") / "state.json"
    rows = []
    if state.exists():
        for i, t in enumerate(json.loads(state.read_text(encoding="utf-8")).get("turns", [])):
            for g in t.get("guards", []):
                rows.append({"turn": i, "session": t["session"], "directive": t.get("directive"), "rev": t.get("rev"),
                             "guard": g, "state": g.get("state")})
    return rows or read_evidence(config)


def cmd_evidence(a) -> int:
    rows = evidence_rows(a.config, a.ga_dir)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("no guard evidence yet (ga writes it when a turn runs with runner.guards)")
    for r in rows:
        g = r.get("guard") or {}
        st = r.get("state")
        print(f"turn {r['turn']} {r['session']} {r.get('directive')} rev {r.get('rev')}: "
              f"allow {g.get('allow', '-')} deny {g.get('deny', '-')} errors {g.get('errors', '-')} "
              f"labels {','.join(g.get('labels', [])) or '-'} | "
              + (", ".join(f"{k}={v}" for k, v in st.items()) if st else "state unknown"))
    return 0


def cmd_upgrade_remote(a) -> int:
    from . import remote

    try:
        old, cmds = remote.upgrade_commands(a.work_repo, a.install_sh)
    except (OSError, ValueError) as e:
        print(f"ga rlo upgrade-remote: {e}", file=sys.stderr)
        return 2
    if not cmds:
        print(f"already at the pinned rlo-sdk ({old[:7]}): nothing to do")
        return 0
    print(f"{a.install_sh} pins rlo-sdk {old[:7]}; ga pins {remote._pins.PINS['rlo'][3][:7]}.")
    print("ga rlo does not edit, commit or push a guard (BD-196). A human runs:")
    for c in cmds:
        print(f"  {c}")
    print("The venv marker carries the PIN, so the worker's next session start reinstalls rlo.")
    return 0


def cmd_preset(a) -> int:
    from . import preset

    model = a.model or str(Path(a.config).resolve().parent / preset.STATE_DIR / preset.MODEL_FILE)
    print(json.dumps(preset.rlo_guard(model), ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, cmd, at = _split(argv)
    if cmd == "hooks":
        return hooks(argv[at + 1:])
    if cmd in GA_COMMANDS:
        print(f"ga rlo: {cmd} is ga's own command: run `ga {cmd} ...`", file=sys.stderr)
        return 2
    a = _parser().parse_args(argv)
    if a.cmd is None:
        _parser().print_help()
        return 0
    return {"init": cmd_init, "doctor": cmd_doctor, "evidence": cmd_evidence, "preset": cmd_preset,
            "upgrade-remote": cmd_upgrade_remote}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
