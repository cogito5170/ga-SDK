"""Session start prompts (METHOD §4b, G8).

The guidance texts are not in code: the config points at the hub's files.
"""
from __future__ import annotations

from .config import Config
from .forms import FormError, Problem


def _guidance(cfg: Config, key: str) -> str:
    """The text of the guidance file ``hub.<key>`` names; a FormError naming the key when it is not set or unreadable."""
    rel = cfg.hub.get(key)
    if not isinstance(rel, str) or not rel:
        raise FormError([Problem(f"$.hub.{key}", "is required by ga prompt (the path of the guidance file)")])
    try:
        return cfg.read_text(rel).rstrip("\n")
    except OSError as e:
        raise FormError([Problem(f"$.hub.{key}", f"cannot read {rel}: {type(e).__name__}")]) from None


def _ownership_table(cfg: Config, session: str) -> str:
    rows = cfg.ownership_rows(session)
    if not rows:
        return "(소유표에 줄 없음)"
    lines = ["| 저장소 | 경로 | 소유 |", "|---|---|---|"]
    lines += [f"| {r['repo']} | `{r['path']}` | {r['session']} |" for r in rows]
    return "\n".join(lines)


def _fmt(template: str, cfg: Config, **extra: str) -> str:
    values = {"hub_name": cfg.hub_name, "hub_repo": cfg.hub["repo"], **extra}
    return template.format(**values)


def worker_prompt(cfg: Config, session: str) -> str:
    s = cfg.sessions[session]
    head = _fmt(cfg.hub["worker_head"], cfg, name=s.name, tag=s.tag)
    guidance = _guidance(cfg, "session_guidance")
    branches = ", ".join(f"{r}@`{s.branch_for(r)}`" for r in s.repos) or "(없음)"
    wake = _fmt(cfg.hub["wake"], cfg, tag=s.tag, link="<글 링크>")
    channel = [
        "## 통로 (ga-SDK 가 설정에서 만든 부분)",
        "",
        f"- 보고 · 질문 · 요청: {s.channel or '(설정에 없음)'} — 보고 하나 = 글 하나, 머리 `[{s.tag}]`. 지시 머리글자는 `CMD-{s.prefix}`.",
        f"- 보고 뒤 깨우기: 허브 세션 `{cfg.hub.get('session_id') or '(설정에 없음)'}` 에 한 줄 `{wake}`.",
        f"- 자기 브랜치: {branches}. 새 일을 시작할 때 통합 브랜치 `{cfg.integration_branch}` 를 먼저 합친다.",
        f"- 기본 통신은 {cfg.hub_name} 하나와만 한다. 다른 세션의 일은 `요청: <대상 세션> …` 으로 허브에 낸다.",
        "- 세션끼리의 교신은 정말 필요할 때만 한다. **교신은 action 이 아니다**: 교신으로 안 것만으로 코드 · 계약 · 소유 파일을 바꾸지 않는다. "
        "교신 직후 자기 통로에 `exchange/1`(from · to · why · asked · got · proposal)로 보고하고, 행동은 허브의 지시로만 시작한다. "
        "어떤 지시에도 속하지 않는 커밋은 통합되지 않는다(R1b).",
        "",
        "### 소유",
        "",
        _ownership_table(cfg, s.name),
    ]
    if s.first_directive:
        channel += ["", "### 첫 지시", "", s.first_directive]
    channel += [
        "",
        "### 꼴 (판 2, METHOD rev 16 §3.6)",
        "",
        "- 지시는 `directive/2` 로 온다: 범위 `scope` 와 끝난 기준 `done_when` 은 항목 목록(S1… · D1…)이다. rev > 1 은 바뀐 것만 `changes` 에 온다.",
        "- 보고는 `report/2` 로 올린다: done_when 항목마다 `items`(met · unmet · blocked · na, evidence), 수치는 `results`, "
        "막힌 까닭은 `blockers`. 머리만으로 판정할 수 있게 쓰고, 본문은 1,500 자 안팎의 요약으로 둔다.",
        "- 깨우는 메시지는 `notify/1` 한 줄이다: `ga notify --to <허브> --kind report --ref <글 URL>`.",
    ]
    tail = _fmt(cfg.hub["worker_tail"], cfg)
    return "\n".join([head, "", guidance, "", *channel, "", tail]) + "\n"


def post_command(cfg: Config, session: str) -> str:
    return cfg.hub.get("post_command", "ga post --channel {session} --from {session} <보고 파일>").format(session=session)


def post_allow(cfg: Config, session: str) -> list[str]:
    """The permission rule that lets a turn run the hub's own report command (and nothing else of ga)."""
    prefix = post_command(cfg, session).replace("<보고 파일>", "").rstrip()
    return [f"Bash({prefix}:*)"]


def report_template(cfg: Config, session: str, directive: dict | None) -> list[str]:
    """The report/1 head a turn fills in: the directive it handles and one commit claim per own repository
    (GA10 F2: without it both first reports claimed no commit, so the hub could integrate nothing)."""
    import json

    s = cfg.sessions[session]
    done = directive.get("done_when") if directive and isinstance(directive.get("done_when"), list) else []
    head = {"schema": "report/2", "from": session,
            "handled": [{"id": directive["id"] if directive else "<지시 id>", "rev_seen": directive["rev"] if directive else 1,
                         "status": "done"}],
            "commits": [{"repo": r, "branch": s.branch_for(r), "sha": "<SHA>"} for r in s.repos],
            "items": [{"id": x["id"], "state": "met", "evidence": ["<sha · 경로 · URL>"]} for x in done]}
    return [
        "- 보고 머리는 이 틀(report/2)을 채운다. `<SHA>` 는 커밋한 머리의 40 자 sha(`git -C <저장소> rev-parse HEAD`)다. "
        "커밋하지 않은 저장소의 줄은 지운다. **커밋을 `commits` 로 주장하지 않으면 허브는 그것을 통합하지 않는다.** "
        "다 못 했으면 `status` 를 `paused` 로 둔다.",
        "- `items` 는 지시의 done_when 항목마다 하나: `state` 는 met · unmet · blocked · na, `evidence` 는 sha · 경로 · URL. "
        "`na` 는 `evidence` 에 해당 없는 까닭을 적는다(까닭이 없으면 met 이 아닌 항목으로 센다). "
        "**항목을 빠뜨린 보고는 받지 않는다(R7).** 수치 결과는 `results`([{name, value, unit?, ci?, evidence}])에, "
        "막힌 까닭은 `blockers`([{kind: env|permission|credential|budget|dependency|design, what}])에 둔다. 본문은 사람을 위한 요약이다.",
        "",
        "```ga",
        json.dumps(head, ensure_ascii=False),
        "```",
    ]


def turn_prompt(cfg: Config, session: str, directive_text: str, directive: dict | None = None) -> str:
    """What a Runner hands to a session for one turn: the directive, how to work, and how to report back."""
    s = cfg.sessions[session]
    if cfg.isolation == "remote":
        # the session runs elsewhere with its own checkout; it pushes its branch and posts on its channel itself
        repos = ", ".join(f"{cfg.repos[r].slug or r} (브랜치 {s.branch_for(r)})" for r in s.repos) or "-"
        how = [
            "## 일하는 방법 (ga)",
            f"- 저장소: {repos}. 네 checkout 에서 그 브랜치로 일하고 커밋한 뒤 그 브랜치만 push 한다. 다른 브랜치는 건드리지 않는다.",
            "- 어떤 명령이 거부되면 다른 길을 찾는다. 같은 명령을 되풀이하지 않는다.",
            "- **일을 다 못 했어도 보고는 반드시 올린다.** 막힌 것은 `## Blocker` 에, 한 것은 `## Result` 에 적는다.",
            f"- 보고: report/1 꼴(```ga 머리 + 본문) 글 하나를 통로 {s.channel or session} 에 올린다. "
            f"글의 마지막 줄은 `<!-- ga-author: {session} -->` 이다.",
        ]
    else:
        repos = ", ".join(f"./{r} (브랜치 {s.branch_for(r)})" for r in s.repos) or "-"
        clone = cfg.isolation == "clone"
        how = [
            "## 일하는 방법 (ga)",
            f"- 현재 디렉터리가 네 자리다. 저장소: {repos}.",
            "- 파일은 Write · Edit 도구로 만들고 고친다(필요한 디렉터리도 저절로 생긴다).",
            "- git 은 `git -C <저장소> …` 꼴로 쓴다(add · commit · status · log · rev-parse).",
            "- push 하지 않는다. 허브가 네 clone 에서 네 브랜치를 가져간다." if clone else "- 자기 브랜치만 push 한다.",
            "- 어떤 명령이 거부되면 위의 방법으로 다른 길을 찾는다. 같은 명령을 되풀이하지 않는다.",
            "- **일을 다 못 했어도 보고는 반드시 올린다.** 막힌 것은 `## Blocker` 에, 한 것은 `## Result` 에 적는다. "
            "보고 없이 턴을 끝내면 허브는 아무 일도 없었던 것으로 본다.",
            f"- 보고: report/1 꼴의 파일을 Write 로 쓰고 이 명령으로 올린다: `{post_command(cfg, session)}`",
        ]
    if directive and directive.get("schema") == "directive/2" and directive.get("rev", 1) > 1:
        # METHOD rev 16 §3.6: the post carries only `changes`; this is the previous revision with them applied
        how = ["## 지금 판 (앞 판에 changes 를 얹은 전체)", "- 범위:"] + [f"  - {x['id']}: {x['text']}" for x in directive.get("scope", [])] \
            + ["- 끝난 기준:"] + [f"  - {x['id']}: {x['text']}" for x in directive.get("done_when", [])] + [""] + how
    how += report_template(cfg, session, directive)
    return (
        f"[{cfg.hub_name} → {s.tag}] 새 지시가 통로 {s.channel or session} 에 왔다. 아래가 그 글이다.\n\n"
        f"{directive_text.rstrip()}\n\n" + "\n".join(how) + "\n"
    )


def hub_prompt(cfg: Config) -> str:
    guidance = _guidance(cfg, "guidance")
    head = (
        f"넌 이제부터 {cfg.hub_name} 세션이야. 허브로서 기준선을 쥐고 지시 · 판정 · 기록을 한다. "
        f"결정의 원본은 <{cfg.hub['repo']}> 레포다. 세션끼리는 직접 말하지 않고 모두 {cfg.hub_name}를 거친다. "
        "단계 마감 · PR · 기본 브랜치 합치기 · 태그 · 기준선 변경은 사용자에게 묻는다. "
        "세션 간 교신(exchange/1)은 정보일 뿐 action 이 아니다: 판정 고리에 넣고, 행동은 너의 지시로만 시작된다."
    )
    sessions = ["| 세션 | 머리글자 | 통로 | 세션 id | 브랜치 |", "|---|---|---|---|---|"]
    for s in cfg.sessions.values():
        branches = ", ".join(f"{r}@`{s.branch_for(r)}`" for r in s.repos) or "-"
        sessions.append(f"| {s.name} | `CMD-{s.prefix}` | {s.channel or '-'} | `{s.session_id or '-'}` | {branches} |")
    owners = ["| 저장소 | 경로 | 소유 |", "|---|---|---|"]
    owners += [f"| {r['repo']} | `{r['path']}` | {r['session']} |" for r in cfg.ownership]
    parts = [
        head,
        "",
        guidance,
        "",
        "## 세션과 통로 (ga-SDK 가 설정에서 만든 부분)",
        "",
        "\n".join(sessions),
        "",
        f"통합 브랜치: `{cfg.integration_branch}` (저장소마다, ff-only).",
        "",
        "### 소유표 (위에서부터 처음 맞는 줄)",
        "",
        "\n".join(owners),
    ]
    return "\n".join(parts) + "\n"


def fresh_instructions(cfg: Config, session: str, directive: dict | None = None) -> str:
    """The fixed part of a fresh turn's prompt (CMD-GA29): how to work and what the final answer must hold. The
    context pack (ga/ctxpack.py) goes before it. English and short: it is read on every turn."""
    import json

    s = cfg.sessions[session]
    repos = ", ".join(f"./{r} (branch {s.branch_for(r)})" for r in s.repos) or "-"
    done = directive.get("done_when") if directive and isinstance(directive.get("done_when"), list) else []
    head = {"schema": "report/2", "from": session,
            "handled": [{"id": directive["id"] if directive else "<id>", "rev_seen": directive["rev"] if directive else 1,
                         "status": "done"}],
            "commits": [{"repo": r, "branch": s.branch_for(r), "sha": "<SHA>"} for r in s.repos],
            "items": [{"id": x["id"], "state": "met", "evidence": ["<sha or path>"]} for x in done]}
    push = "Do not push; the hub fetches your branch." if cfg.isolation == "clone" else "Push only your own branch."
    return "\n".join([
        f"## How to work ({cfg.hub_name} -> {s.tag})",
        "- The pack above is all you get: there is no earlier conversation. Read only the files you need, in parts.",
        f"- The current directory is your workspace. Repositories: {repos}. Use `git -C <repo> ...`. {push}",
        "- If a command is refused, find another way; do not repeat it.",
        "- Do not post anything. Your final answer is the report: the hub checks it, posts it and keeps your state block.",
        "",
        "## Final answer (required, even when blocked)",
        "1. A report/2 post: a ```ga block with this head filled in (`<SHA>` = the 40-hex commit sha; drop repos you "
        "did not commit to; status `paused` if not finished; one item per done_when id: met | unmet | blocked | na), "
        "then a short body with `## Result` (and `## Blocker` if any).",
        "```ga",
        json.dumps(head, ensure_ascii=False),
        "```",
        "2. One ```state block: what is done, what is next, and anything the next turn must know (it has no other memory). "
        f"At most {s.state_max_tokens * 4} bytes.",
        "",
    ])
