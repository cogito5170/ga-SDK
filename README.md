# ga-SDK

허브 세션 하나가 지시와 보고로 작업 세션 여럿을 굴리는 고리를 기계로 돌린다. 명세는 [`METHOD.md`](METHOD.md)(baseline 소유)에 있고, 짓는 방법은 [`DESIGN.md`](DESIGN.md)에 있다.

1판(0.1)은 로컬에서만 돈다. 쓰는 것은 파일 우편함, git worktree, venv, 수동 Runner뿐이다. 원격 세션, GitHub, LLM, 네트워크 없이 돌아간다. Python 3.10 이상이 필요하고, 핵심은 표준 라이브러리만으로 짓는다.

## 설치

```sh
pip install -e .
```

- **One ga (CMD-GA20, BD-206):** installing ga-sdk also installs `rlo-sdk[sensor]`, pinned by sha (`ga/_pins.py` is the source; `pyproject.toml` must say the same, and `tests/test_unified.py` checks both, plus the installed metadata in a clean venv). rlo's seven layers stay in their own repositories; ga copies none of their code.
- ga core still imports only the standard library and never imports rlo. rlo comes in only through `ga rlo ...`.
- **`ga rlo ...`** hands everything after `rlo` to `ga.rlo.cli.main(argv)`, unchanged (`--help` included). ga's own `--config` / `--ga-dir` before `rlo` are not passed on. If that module is not there, it exits 2 with `ga rlo: module ga.rlo.cli is not there yet`. If one of its imports is missing, it exits 2 naming that module. An error raised inside `main` is not caught.
- **Ownership:** `ga/rlo/*` and `tests/test_rlo/*` belong to GR (the ga_rlo session). GA does not edit them; a change there goes to GR as a `요청:`. GA owns the seam's entry (`ga/__main__.py` `cmd_rlo`) and the pins (`ga/_pins.py`).

## 한 바퀴

1. 설정 파일 `ga.json`(꼴은 `ga-config/1`)에 다음을 적는다: 저장소(로컬 경로), 세션, 소유표, 통합 브랜치. 예는 [`examples/baseline/config.json`](examples/baseline/config.json)에 있다.
2. 세션을 만들 때 첫 프롬프트를 `ga prompt <세션>`으로 찍는다. 허브의 첫 프롬프트는 `ga prompt --hub`로 찍는다.
3. 허브가 지시를 보낸다: `ga send directive.md`.
   - 수동 Runner가 `.ga/outbox/<세션>/`에 붙여 넣을 프롬프트를 쓴다.
   - 세션 worktree는 `.ga/worktrees/<세션>/<저장소>`에 생긴다. 남의 브랜치로 push하면 pre-push 훅이 막는다.
4. 세션이 일한 뒤 보고한다: `ga post --channel <세션> --from <세션> report.md`.
5. 허브가 한 바퀴 돈다: `ga tick`. 하는 일은 다음 순서다: 받기, 통합(ff-only), 재현(경로 방식과 설치 방식), 판정, 기록, 다음 고르기.
   - 판정은 사람이 `.ga/judge/round-<n>.json`에 적는다. 무엇을 적을지는 `round-<n>.request.md`에 나온다.
   - 게이트에 걸리면 `.ga/questions/`에 질문이 생긴다. 사용자는 `ga answer <질문> <선택지>`로 답한다.
6. 새 것이 없을 때 `ga tick`은 아무것도 쓰지 않는다. 그래서 cron에 걸어 안전망으로 쓸 수 있다.

## 헤드리스 Runner (2판 첫째)

설정에 `"runner": {"kind": "headless", "model": "haiku", "max_budget_usd": 0.5}`를 넣으면, `ga send`와 `ga tick`이 지시를 보낼 때 작업 세션의 한 턴을 `claude -p`로 직접 돌린다.
- 턴이 끝나면 세션 id를 받아 두고, 다음 턴은 `--resume`으로 이어 간다. 턴마다 비용과 시간을 `.ga/state.json`에 수로만 남긴다.
- 자식 실행은 깨끗한 환경에서 돈다. HOME과 `CLAUDE_CONFIG_DIR`은 `.ga/headless/home` 아래이고, 이 프로세스의 `CLAUDE_CODE_*` 변수는 넘기지 않는다. 권한은 `--permission-mode acceptEdits`와 허용 도구 목록으로 좁힌다.
- 설정의 `budget`(예: `{"runs": 6, "cost": 1.0}`)을 넘을 턴은 실행하기 전에 멈추고 게이트 6으로 간다.
- 실제 실행 기록은 [`examples/verify/headless_results.json`](examples/verify/headless_results.json)에 있다.

## 받는 쪽 R3 와 LLM Judge

- `ga setup`: 로컬 bare 원격에 pre-receive 훅을 단다. 남의 브랜치 push, force push, 지우기는 `--no-verify` 로 보내도 원격에서 막힌다.
- 설정에 `"judge": {"kind": "llm", "model": "haiku", "max_runs": 10}` 을 넣으면 `claude -p` 가 판정과 다음 지시 초안을 제안한다. 기계의 클래스와 게이트가 그 위에 선다.
- 실제 실행 기록은 [`examples/verify/ga3_results.json`](examples/verify/ga3_results.json) 에 있고, 재생에 쓴 사례는 [`judge_cases.json`](examples/verify/judge_cases.json) 이다.

## 턴 진단 라벨

허브는 턴마다 다음 네 가지를 수와 라벨로만 남긴다: 커밋했나 · 보고가 몇 개 왔나 · 형식이 맞는 보고가 몇 개인가 · 주장한 sha 를 허브가 아는가. 저장 위치는 `.ga/state.json` 의 `turns[].diag` 다.
- 턴의 끝을 아는 Runner(헤드리스 · Agent SDK)는 턴이 끝나자마자 남긴다.
- 끝을 모르는 Runner(수동 · 원격)는 그 세션이 글을 올린 다음 tick 에서 남긴다.

## Channel · Runner

| 종류 | 이름 | 상태 | 쓰는 법 · 보장 |
|---|---|---|---|
| Channel | 파일 우편함(`FileMailbox`) | 1판 | `.ga/mailbox/<세션>/`. 샌드박스 모드에서 세션은 자기 우편함에만 쓸 수 있다 |
| Channel | GitHub 이슈(`GitHubIssueChannel`) | 2판 넷째 | 세션마다 이슈 하나, 글 하나 = 댓글 하나. 표준 라이브러리만 쓴다. `Link: rel="next"` 를 따라 읽고, 속도 제한(429 · 403+남은 0)은 `RateLimited(retry_after)`, 그 밖의 실패는 `ChannelError(status)`. 토큰은 요청마다 환경 변수(기본 `GITHUB_TOKEN`)에서 읽고, 저장 · 기록하지 않는다. 작성자는 댓글 끝 쪽의 `<!-- ga-author: X -->` 줄(없으면 로그인) |
| Channel | 콜백(`CallbackChannel`) | 2판 넷째 | 라이브러리가 GitHub 에 직접 닿지 못하는 곳에서, 닿을 수 있는 쪽(에이전트의 도구, 사람)이 `post` · `read` 를 준다 |
| Runner | 수동(`manual`) | 1판 | 붙여 넣을 프롬프트를 쓴다. 턴의 끝을 모른다 |
| Runner | 헤드리스(`headless`) · Agent SDK(`agent_sdk`) | 2판 첫째 · 셋째 | 턴을 직접 돌리고 끝 · 비용 · 세션 id 를 안다. R3 는 아래 표 |
| Runner | 원격 세션(`RemoteSessionRunner(send)`) | 2판 넷째 | 한 턴 = 클라우드 세션에 보내는 메시지 하나. 세션을 만들고 깨우는 공개 API 를 라이브러리가 부를 수 없어서, 깨우기는 콜백이다(`OutboxCallback` 은 요청을 파일로 써 둔다). 턴의 끝 · 비용을 모른다(`ended: null`, `cost_unknown_runs`). 세션의 보고와 안전망 `ga tick` 이 고리를 잇는다. `isolation: "remote"` 와 함께 쓴다 |

## R3 는 무엇으로 지켜지나 (Runner 모드별)

R3: 세션은 자기 브랜치만 내고, 통합 브랜치는 허브만 움직인다. 기본 배치는 `isolation: "clone"` 이다.
- 세션마다 독립 clone 을 쓴다. ref 를 나누지 않고, 객체도 하드링크하지 않는다.
- 세션은 push 하지 않는다. 허브가 각 세션의 clone 에서 **그 세션의 브랜치만** 가져온다(pull).
- 그래서 "누가 썼나" 는 세션이 주장하는 이름이 아니다. 허브가 어디서 가져왔느냐로 정해진다.

| 모드 | 지켜 주는 것 | 못 지키는 것 |
|---|---|---|
| **헤드리스 + 쓰기 샌드박스**(Linux, 비특권 user namespace. `ga.adapters.sandbox.available()`) | 턴 안의 모든 프로세스에서 다음이 **읽기 전용**이다(OS 수준, 잠긴 마운트): 설정 디렉터리, `.ga`, 모든 저장소, 로컬 원격, 다른 세션의 clone · 우편함. 쓸 수 있는 곳은 자기 clone, 자기 우편함, 자기 Claude 홈뿐이다. 따옴표로 나누기 · 옵션 줄임 · 스크립트 · 다른 언어 · 다시 마운트 · 새 namespace 로도 원격과 허브의 ref 를 바꾸지 못한다(시험: 공격 15 가지 모두 실패) | 읽기는 막지 않는다(다른 세션의 파일이 보인다). 네트워크와 CPU 는 그대로다. user namespace 가 꺼진 배포판, macOS, Windows 에서는 샌드박스가 없다. 이때 `sandbox: "require"` 는 턴을 돌리지 않고, `"auto"` 는 샌드박스 없이 돌린 뒤 턴 기록에 `sandboxed: false` 를 남긴다 |
| **Agent SDK Runner + 쓰기 샌드박스**(`runner: {"kind": "agent_sdk"}`, `pip install "ga-sdk[agent]"`) | 헤드리스 + 샌드박스와 같다. SDK 가 띄우는 CLI 를 ga 의 래퍼(`cli_path`)가 감싼다. 래퍼는 `env -i` 로 허락한 변수만 넘기고, 샌드박스 안에서 실행한다. 래퍼는 턴이 쓸 수 없는 곳에 있고, 턴마다 다시 써진다. 가드는 `settings`, 사용자 설정 파일은 `setting_sources=[]` 로 다룬다 | 헤드리스 + 샌드박스와 같다. 또 SDK 가 없으면 턴을 돌리지 않고 `sdk_not_installed` 로 기록한다 |
| **헤드리스, 샌드박스 없음** | 허브가 pull 하므로 신원을 주장할 일이 없다. 문자열 가드(PreToolUse)가 첫 겹으로 흔한 우회를 막고, pre-receive 가 이름 없는 push 를 막는다. 허브는 통합 브랜치가 자기 모르게 움직이면 알아챈다(R3 알림) | **보장하지 않는다.** 같은 사용자 권한이므로 셸을 마음대로 쓰는 턴은 원격 · 허브 저장소 · 다른 세션의 clone 과 우편함 · 허브 상태를 직접 고칠 수 있다(시험: 샌드박스를 끄면 공격 15 가지 중 11 가지가 통과). 가드는 문자열 규칙이라 완전하지 않다 |
| **수동**(사람이 세션 터미널에서 일함) | `ga sandbox <세션>` 으로 연 셸은 헤드리스 + 샌드박스와 같은 보장을 받는다 | 그 밖의 보통 셸은 "샌드박스 없음" 과 같다. 사람은 가드 밖에 있다 |
| `isolation: "worktree"`(0.1 방식) | pre-push(보내는 쪽)와 pre-receive(받는 쪽)가 실수를 막는다 | worktree 의 git 데이터가 허브 저장소 안에 있어서 샌드박스를 걸 수 없다. 받는 쪽 명령(`--receive-pack`)도 push 하는 쪽이 고른다. **구조로 지켜지는 R3 는 없다** |
| `isolation: "remote"` + 원격 Runner(클라우드 세션, 2판 넷째) | 세션은 다른 기계에서 자기 checkout 으로 일하고 자기 브랜치를 원격에 push 한다. 허브는 원격을 fetch 해 읽기만 하고, `push: false` 면 통합 브랜치를 허브의 로컬 저장소에서만 움직인다(원격의 통합 브랜치를 건드리지 않는다). R1b 는 그대로다: 보고로 주장하지 않은 커밋은 통합되지 않는다 | **ga 가 지키는 R3 는 없다.** 로컬 샌드박스 · 가드 · pre-receive 가 세션 쪽에 닿지 않는다. 세션이 남의 브랜치에 push 하는지는 플랫폼(GitHub 브랜치 보호, 원격 세션의 push 허용 브랜치)이 정한다. 한 계정으로 허브와 세션을 다 쓰면 GitHub 로그인으로 작성자를 가를 수 없어, 댓글의 `<!-- ga-author: X -->` 줄(주장일 뿐, 증명 아님)로 가른다 |

더 강하게 지키려면 원격이 사람을 직접 확인해야 한다(예: GitHub 브랜치 보호). 세션마다 OS 사용자 · 컨테이너를 따로 두는 길도 있다.

## 판정 바닥 (METHOD rev 9)

- **알려진 건너뜀:** 저장소 설정 `"expected_skipped": {"count": 2, "why": "설치 메타데이터 시험"}`. 건너뜀이 정확히 그 수이고 까닭이 있으면 바닥에 넣지 않는다. 다른 수이거나 까닭이 없으면 `partial · measurement · skipped` 다.
- **포장된 저장소**(`pyproject.toml` · `setup.py` · `setup.cfg`)를 깨끗한 설치(`Bundle` (b), 설정에 `package`) 없이 재현하면 `partial · measurement · not_install_checked` 다.
- (b) 는 시험과 별도로, 깨끗한 venv 에서 빈 디렉터리를 작업 디렉터리로 두고 설치된 배포의 패키지 아래 **소스의 모든 모듈**을 import 한다. 시험은 checkout 에서 돌아 그 사본이 설치본을 가릴 수 있기 때문이다. 소스에 있고 설치본에 없는 모듈은 `failure · implementation`(`missing_in_install`) 이다.
- **R4(ff 아님)** 는 게이트가 아니다. 통합하지 않고 그 세션 몫을 `partial · requirement` 로 둔 뒤, 허브가 그 지시의 rev+1(통합 브랜치를 합치고 다시 보고)을 스스로 보낸다.
- 턴 프롬프트에는 채울 report/1 머리 틀이 들어간다(지시 id · rev, 자기 저장소마다 브랜치와 `<SHA>`).

## 바깥 판정과 빈 초안 (METHOD rev 10)

- **바깥 판정:** `ga review --by baseline --repo rlo --sha 6c33b85 --class partial --cause implementation --why "…"`.
  - `review/1` 기록을 덧붙인다. 그 sha 를 통합한 회차의 판정은 더 엄한 쪽으로만 고친다(`amends`). 회차 기록 자체는 다시 쓰지 않는다. `ROUNDS.md` 에는 그 회차 밑에 한 줄로 보인다.
  - 다음 tick 은 그것만으로도 회차를 돈다. Judge 문맥(`outside_reviews`)과 근거 note 에 들어간다. 그 sha 가 아직 통합 머리면 기계 바닥이 된다.
- **빈 초안 메우기:** Judge 가 refine · verify 를 내면서 지시 초안을 비우면, 허브가 세션마다 rev+1 초안을 만든다. 근거는 판정 · 근거 note · 주장과 증거의 차이 · 바깥 판정이다. `directive/1` 검사를 통과한 것만 보낸다.
  - 커밋했지만 보고에서 주장하지 않은 세션은 근거 note 에 `R1b: … claims no commit` 으로 남는다.
- **설치 환경:** (b) 의 근거 note 에 `bundle (install): built with python · pip · setuptools · wheel` 판이 남는다.

## Judge 의 ask_user (METHOD rev 11)

- Judge 의 ask_user 는 §6 게이트 일곱 가운데 하나를 `"gate": 1..7` 로 댈 때만 질문(게이트)이 된다. 근거가 없거나 일곱 밖이면 회차 알림으로 남고, 그 회차의 다음은 wait 다. 근거가 있으면 반드시 멈춘다. 게이트를 끄는 설정은 없다.
  - **Judge 자체가 실패한 회차**(모형을 부를 수 없음 · 무효한 답, METHOD rev 12): 기계의 클래스로 판정하고 게이트는 없다. 다음 tick 에서 그 회차의 증거로 Judge 를 한 번 다시 부른다. 새 보고가 와서 보통 회차가 돌면, 그 회차의 Judge 호출이 다시 부르기다. 예산 `judge_runs` 를 넘으면 부르지 않는다.
    - **연달아 두 회차 실패하면 게이트 6** 으로 멈춘다. 예산이 다 돼 다시 부를 수 없을 때도 같다. 사이에 성공이 한 번이라도 끼면 셈은 처음부터다.
- 열린 바깥 판정(`review/1`)이 있는데 Judge 가 wait 를 내고, 그 sha 를 낸 세션에 열린 지시도 없으면 soft 알림을 남긴다. 판정은 바꾸지 않는다.

## 모형 턴을 여는 Runner 의 허락 (METHOD rev 13 §4c)

- 헤드리스 · Agent SDK · 원격 Runner 는 **사람의 허락이 있어야** 첫 턴을 연다. 수동 Runner 는 모형을 부르지 않으므로 허락이 필요 없다.
- **옮기는 법(기존 설정):** 사람이 `ga permit --runner agent_sdk --model haiku --sandbox require [--budget runs=8] [--measurement-calls]` 로 허락을 남긴다(`decision/1`, by user, `scope`). 나온 id 를 설정에 넣는다: `"runner": {"kind": "agent_sdk", ..., "permission": "BD-n"}`. 이 칸이 없는 설정은 그 Runner 들에서 게이트 6 으로 멈춘다.
- 허락의 범위(Runner 종류 · 모형 · 샌드박스 · 예산) 밖이면 보내지 않는다. 지시는 상태에 `not_sent` 로 남고, 통로에는 아무것도 쓰지 않으며, 게이트 6 질문(허락한다 · 수동으로 강등한다 · 멈춘다)이 생긴다.
- 실행이 거부되면(`refused:…`) 그 지시는 `not_sent` 로 남고 실행 수에 넣지 않는다. 게이트 6 으로 묻고, **다른 길로 다시 시도하지 않는다.** 보낸 뒤 예외가 나도 상태를 먼저 남긴다.
- **수동 강등은 사람의 답으로만 한다.** `ga answer <질문> 수동으로 강등한다` 를 하면 같은 지시를 수동 Runner 로 내고, 기록에 `runner: manual` · 강등 전 Runner · 까닭 · 결정 id 를 남긴다. 기본 권고는 "멈춘다" 다.
- **`by: user` 는 기록이지 신원 증명이 아니다.** 운영자는 사람의 직접 답을 받은 뒤에만 `ga permit` 을 하고, `--note` 에 그 답의 요지를 적는다(METHOD rev 14, BD-158).
- **LLM Judge 도 같은 허락으로 막는다**(rev 14 §4c 6). 허락 범위에 `measurement_calls` 가 없으면 Judge 를 부르지 않는다. 그 회차는 기계의 클래스로 판정하고 근거 note 에 "Judge 허락 없음" 을 남긴다.
  - 게이트 6 으로 한 번 묻는다. 그 질문이 열려 있는 동안은 다시 묻지 않는다. rev 12 의 재시도 · 연속 실패 셈에는 넣지 않는다.
  - Runner 를 허락할 때 `--measurement-calls` 를 붙이면 함께 허락된다. 수동 Runner 허브는 `ga permit --judge-only` 로 Judge 만 허락한다(`scope.runner: manual`). 이 허락으로는 모형 턴 Runner 를 열 수 없다.
  - 모형을 부르지 않는 Judge(`FileJudge` · `CallableJudge`)는 허락이 필요 없다.

## 작업 턴의 운영자 가드 — 중간 검증 라인 (METHOD rev 15 §4c 7)

- 설정 `runner.guards: [{"command", "matcher"?, "record"?, "name"?}]`(헤드리스 · Agent SDK 만). 작업 턴 전용 설정(`<Runner 집>/<세션>/ga-settings.json`)의 PreToolUse 에 **ga 가드 다음** 순서로 들어간다. 사람의 `~/.claude` 와 허브 세션 설정은 건드리지 않는다.
  - 명령과 기록 경로에 `{session}` · `{home}`(그 세션의 집, 샌드박스 안에서도 쓸 수 있음)을 쓸 수 있다. 예:
    `{"name": "rlo", "command": "python3 -m rlo.hooks --model {home}/cc_tools_model.json --mode enforce --grant Bash --record {home}/rlo-{session}.jsonl", "record": "{home}/rlo-{session}.jsonl"}`
  - ga-SDK 는 rlo 에 의존하지 않는다. 명령 문자열만 받는다.
- `record` 가 있으면 턴 뒤에 그 턴 동안 늘어난 줄만 읽는다. 허락 · 거부 · 오류 수와 거부 라벨만 `turns[].guards`(와 `diag.guards`)에 싣고, 원문은 싣지 않는다. 거부가 있으면 그 세션의 보고가 들어온 회차의 근거 note 에 남는다.
- State lines (CMD-GA19): a guard record line `{"kind": "state", "labels": {name: value}}` puts its labels in `turns[].guards[].state` (and `diag.guards[].state`); a later line's value wins. Only short label names and values are kept (`[A-Za-z0-9_.:+-]{1,40}`, or a number, bool or null); any other entry is dropped and the line counts once in `errors`. No state line, no `state` field.
- 가드의 프로그램이 없거나 실행할 수 없으면 턴을 열지 않는다. 지시는 `not_sent` 로 남고 게이트 6 이다(가드를 고친다 · 수동으로 강등한다 · 멈춘다). 가드 없이 조용히 돌지 않는다.
- 가드는 허락(§4c 1)도, 실행 환경의 권한 검사도 대신하지 않는다.

## 꼴 판 2 (METHOD rev 16 §3.6)

- **`directive/2`:** `scope` · `done_when` 은 항목 목록이다(`[{id: "S1"|"D1", text}]`, 한 항목에 한 요구 · 따로 확인할 수 있는 기준). 선택 칸으로 `refs` 가 있다. **rev > 1 은 `changes`(`[{item, op: add|edit|drop, text?}]`)만 보낸다.** 허브는 앞 판에 그것을 얹어 전체 판을 기록하고, 턴 프롬프트에도 "지금 판" 으로 보인다(`ga.forms.apply_changes`). 없는 항목을 고치거나 있는 항목을 더하면 보내지 않는다.
- **`report/2`:** report/1 의 칸에 더해 `items`(다룬 지시의 done_when 항목마다 met · unmet · blocked · na, evidence)를 둔다. 선택 칸으로 `results`([{name, value, unit?, ci?, evidence}]) · `blockers`([{kind: env|permission|credential|budget|dependency|design, what, gate?}]) · `deviations` · `proposals` 가 있다. 본문은 1,500 자 안팎의 요약이다(soft).
- **`notify/1`:** 세션을 깨우는 한 줄. `ga notify --to <세션> --kind directive|report|verdict|question|ack --ref <URL> [--id]`.
- **기계 규칙**
  - done 으로 보고했는데 met 이 아닌 항목이 있으면 바닥은 부분 성공이다.
  - `na` 는 `evidence` 에 까닭이 있으면 바닥에서 뺀다(round note 에 남음). 까닭이 없으면 met 이 아닌 항목으로 센다(METHOD rev 17, CMD-GA19).
  - blocked 항목이 있으면 바닥은 막힘이다(원인은 blockers 의 kind 로 정함).
  - blockers 의 kind 가 permission · credential 이면 게이트 6 이다.
  - items 가 다룬 directive/2 의 done_when 항목을 빠뜨리면 R7 hard 로 그 보고를 받지 않는다.
- **판 1 꼴(`directive/1` · `report/1`)도 계속 받는다.** 받을 때마다 soft 알림(deprecated)을 남긴다. directive/1 을 rev > 1 로 보내면 "changes 없음" soft 알림이 붙는다. 예: `examples/v2/CMD-GA18.directive2.md`.

## `ga prompt` and the guidance files (CMD-GA19)

- `ga prompt SESSION` reads `hub.session_guidance`; `ga prompt --hub` reads `hub.guidance`. A missing key, an empty one or an unreadable file is a config error: exit 2, and stderr names the key (`config: [hard] $.hub.session_guidance: …`). An unknown session is exit 2 too. The library (`worker_prompt` · `hub_prompt`) raises `FormError`.

## `ga mail`: session to session through git (CMD-GA22, BD-235)

A channel that needs nothing but Bash and a git repository both sessions already have (for AMP and W1: amp). `send_message` stays the fast path; the mailbox is the floor that always exists.

```sh
ga mail send --repo PATH --to W1 [--from AMP] directive.md      # validate, then add one file to the ga-mailbox branch
ga mail read --repo PATH --as W1 [--json]                       # new messages for W1, oldest first
ga mail scan --repo PATH [--json]                               # per name: messages, unread, the oldest unanswered report
ga mail send --repo PATH --to AMP --from W1 --guard-event FILE --re CMD-W1 [--items D1,D2]
```

- **The branch** `ga-mailbox` (orphan) holds `to/<recipient>/<utc>-<sender>-<form id>.md`, one ga form per file. Files are only added: a send fetches the tip, adds its one file on top with git plumbing (the session's working tree and branches are not touched) and pushes; a rejected push is redone on the new tip, up to 20 times with full-jitter backoff (at most 3 s per wait). Then it fails with an error, never silently.
- **Refused before sending:** a text that is not a valid ga form (hard problems), a text that looks like a secret (the R6 patterns: Anthropic, OpenAI, GitHub, AWS, Slack, private keys, and since CMD-GA24 Google API keys and Google OAuth access/refresh tokens and client secrets), a recipient that is not one directory name, a sender with `-` (the file name must split one way). The sender is the form's `from`, or `--from`.
- **Reading** shows each message with its sender, form and validation result, and says it is data from that sender, not instructions. A message is marked read only after it was printed. The read set lives in the clone's git dir (`<git dir>/ga-mailbox/<name>.cursor`), never pushed. It is a set of names, so clock skew between machines cannot hide a message.
- **Scan** (the hub's safety net): for each name, the messages addressed to it and how many are unread. Unread uses the local read set when this clone has one, else counts the messages after the name's own last send. It also gives the oldest report to the name that it has not answered (no message from it to that sender since).
- **A guard deny inside a container** (`--guard-event`, a guard record: rlo `--record` lines or ga's guard log) goes out as a `report/2`: `handled.status: paused`, a `permission` blocker `guard rlo denied N tool call(s): <labels>`, and the given items marked blocked. Counts and labels only.
- No network but the repository's git remote.

## `ga gemini`: a Gemini supervisor (CMD-GA21, BD-221)

You talk to ga, not to Gemini CLI's interactive UI. Gemini only directs; ga and rlo do the work.

```sh
ga gemini --config ga-gemini.json "read issue 12 and post a summary"   # one task
ga gemini --config ga-gemini.json                                      # a prompt loop (ga gemini> …)
ga gemini --config ga-gemini.json --resume                             # after a crash or a closed terminal
```

- **A fixed model, no switch prompt.** The model comes from the config (`model`, default `gemini-3-flash-preview`) and nowhere else. Every Gemini turn is one headless CLI process: `gemini -p … --output-format stream-json -m <model> [--resume <session>]`, with stdin closed, so the quota dialog never opens. The served model is every key of the result's `stats.models` (`init.model` is only the model asked for). If it names another model, or none, the turn fails. It is never accepted and never retried on another model. ga runs every turn of a task in the config's directory, because the CLI keeps sessions per project directory, and hands the CLI private settings with `general.maxAttempts: 1`: the system settings in force plus that one key, passed through `GEMINI_CLI_SYSTEM_SETTINGS_PATH` (system settings win the merge). Your own settings files are not touched. **This does not stop `gemini-3-flash-preview` from retrying:** the CLI uses the model policy's `maxAttempts` first, and the preview chain gives that model 10. So one turn can retry a per-minute 429 up to 10 times inside the CLI. The setting still binds models outside that chain (BD-232).
- **Gemini only directs.** Each turn answers with one closed step list (`ga-gemini-plan/1`): tool steps from the config's `tools` table, with `args` and `after`, and at most one `next` model step that receives the results it lists. A tool outside the table, or any other shape, fails that model step and nothing of it runs. Tools are an extension's MCP tools, called directly over stdio (`mcp_servers`), or Python functions (`"python": "module:function"`).
- **rlo runs the list** (K12 `Scheduler` and `Governor`, `budget` = `{rpm, tpm}` per minute; the default `{"rpm": 5}` is meant to sit below the server's per-minute limit so the CLI rarely meets a 429 — the free tier's exact limit is not known here, so set your key's limit minus a margin). Tool steps run whenever they are ready; a parked model step never holds them up. A model step goes out only when the Governor allows. A quota error is read from the result's `error.type`: `RetryableQuotaError` parks the step for a "retry in N s" hint in its message, or else the Governor's minute window. `TerminalQuotaError`, or a per-day quota in the message, is the daily quota (below). A quota sentence in the text of another error class is a second path only. The exit code is never read; a turn with no `result` event is a crashed turn.
- **When the quota is hit**, ga prints one block and saves the state:
  ```
  [ga gemini] quota: T1.m2 parked — resumes in 7 s, at 14:52:07 (hint)
    now:  done 5 · running 0 · parked 1 · requests today 2 (no daily cap)
    next: T1.m2 (model)
    saved: …/.ga-gemini/state.json — after a crash or a closed terminal: ga gemini --resume
  ```
  It then sleeps until the window opens and resumes by itself. `--resume` keeps the rest of a saved wait.
- **A long turn** (longer than `turn_status_s`, default 20 s) gets one line per interval, because the CLI may be retrying a quota error inside the turn:
  ```
  [ga gemini] turn T1.m2 running 40 s; the CLI may be retrying a quota error inside the turn · done 3 · running 1 · parked 0
  ```
- **`max_parallel`** (tool steps at once, default 4) is handed to rlo's Scheduler (K12 rev 3): ready tool steps run concurrently in a pool of that size.
- **The daily quota.** By default ga sets **no daily cap**: the free tier's 20 a day is gone with billing on (CMD-GA26 S6, BD-294), and a cap is the integrating side's choice. `daily` = {`requests`, default none; `reset_tz`, default `America/Los_Angeles`; `reset_at`, default `00:00`}. ga counts the requests it sends today (`day.json`). When a configured cap is used up, or the server says `TerminalQuotaError` (with or without a cap), the step waits for the reset instead of spending a call on a known 429. The per-minute Governor (`budget`) works as before:
  ```
  [ga gemini] daily quota: T1.m3 parked — the quota resets at 00:00 America/Los_Angeles, in 15 h 0 min (at 08:00:00 here); one probe then
    now:  done 4 · running 0 · parked 1 · requests left today 0 (the server's daily quota)
  ```
  At the reset ga sends that one step once (the probe). If it hits the quota again, it waits for the next reset; it never retries in a loop. Tool steps keep running meanwhile. ga counts only its own requests; other sessions on the key are why the server's word wins.
- **On disk** (`state_dir`): `state.json` (session id, steps, done, parked), `results/<step>.json` (full tool results), `log.jsonl` (labels and numbers only) and `ledger.jsonl` (rlo's rows). In memory, each result is capped at `result_cap` characters. No Node process lives for the whole session.
- Example config: `examples/gemini/ga-gemini.json`. Needs Gemini CLI and its credential in the environment (for example `GEMINI_API_KEY`); ga never stores it.

## `ga gemini --host agy`: the same loop on Antigravity CLI (CMD-GA23, BD-234/255)

`ga gemini --host agy` (or `"host": "agy"` in the config) runs each model step as one headless `agy` process with your Google account: `agy -p … --output-format stream-json --model gemini-3.8-flash-high`, in the config's directory (the workspace with the extension's `.agents`), stdin closed. The default host stays Gemini CLI; the model rule is the same: the slug in `agy.model` and nothing else, and a turn served by any other model fails.

- **Quota:** agy's quota is a weekly share per model family, not requests per minute. Before a model step ga reads `agy -p /usage` (it spends nothing), at most every `usage_every_s`. When the family's remaining share is under `usage_floor_pct`, the step waits for the reset agy reports:
  ```
  [ga gemini] agy quota (gemini, weekly): T1.m2 parked — 3% left (floor 5%); resets at 2026-10-11 00:56 KST, in 6 d 7 h 56 min; one probe then
  ```
  A quota stop (`AGY_ERROR`, exit 3) waits the same way, with one probe at the reset. Tool steps keep running.
- **AI credits are never accepted.** When agy offers paid credits ("Use AI Credits") or says the credits balance is too low, ga treats the plan quota as spent and waits for the reset. Spending money is your decision; ga passes nothing that could enable credits and closes stdin, so no prompt can be answered.
- `denied_actions` (tool calls agy refused) are printed and logged, as labels and counts.
- Every turn carries the protocol and the task again, because agy has no known `--resume`.
- ga passes nothing for auth and never reads the token store (keyring, `~/.gemini/antigravity-cli/…`).
- Example: `examples/gemini/ga-gemini.agy.json`.

### What the Mac run must settle (HUMAN_QUEUE Q5)

The output shapes are assumptions until a real agy shows them. Run these on the Mac, in the workspace, and paste the output back. Never paste a token; `ga mail` and R6 refuse one anyway.

1. `agy --version`, and `agy --help`: does it list `--resume` or `--input-format`?
2. `agy models --output-format json`: the field names, and that `gemini-3.8-flash-high` is there.
3. `agy -p "/usage"` and `agy -p "/quota"`: the exact lines (percent, family, reset time and zone, the words "weekly" or "5-hour").
4. `agy -p "Reply with the word OK" --output-format stream-json --model gemini-3.8-flash-high; echo "exit $?"`: the raw lines. Where is the model, the text, a session id?
5. The same with `--output-format json`.
6. A turn where a tool is refused (for example a fetch the workspace's `.agents` rules deny): the `denied_actions` entry.
7. If a quota stop happens on its own (do not force one): the exact `AGY_ERROR` line and the exit code. Never answer yes to "Use AI Credits".
8. End to end: `ga gemini --host agy --config ga-gemini.agy.json "list the tools you can use"`, plus `.ga-gemini/log.jsonl` (labels and numbers only).

## `ga gemini` prompts: compact follow-ups and a token report (CMD-GA26)

ga's Gemini prompts come from one spec, `ga/specs/gemini-plan.pspec` (prompt-spec/1, read by `rlo.pspec`). The first turn is today's text. Follow-up turns are compact by default. On baseline's 8-turn conversation this cuts the estimated prompt tokens (bytes/4) from 646 to 531 on Gemini CLI, and from 2263 to 1658 on agy, which resends the protocol and the task every turn. To send today's text on every turn, set `"prompt_mode": "verbatim"` in `ga-gemini.json` or pass `--prompt-mode verbatim`.

```
ga gemini --token-report tests/fixtures/pspec/conversation_hero8.json   # offline: tokens per turn, verbatim vs compact
```

## Messages: one compact head, prose on demand, one read path (CMD-GA27)

A posted directive, report, verdict or notify is one ```ga block of minified JSON plus the footer: no prose (`ga check` says `wire:prose` / `wire:pretty`, soft). A human note goes in `note` (at most 280 characters).

```
ga notify --to baseline --kind report --ref <comment url> --wire   # the posted notify
ga render report.md --lang ko                                      # prose for people, from ga/specs/forms; no model call
ga inbox github:cogito5170/baseline#12 --repo .                    # only what is new since the cursor, heads only
ga inbox mail:GA --repo .                                          # the same for ga mail
ga wire-report tests/fixtures/wire/channels.json                   # offline: bytes now vs the wire form
```

`ga inbox` keeps its cursor in the git dir (never pushed) and moves it only after its output is written. Read a notified message through `ga inbox`, not the notify body and the issue both.

## Fresh turns with a bounded context (CMD-GA29, BD-296/303)

A session set to `"context": "fresh"` (with `"pack_max_tokens"`, required) never resumes: every turn is a new
`claude -p` (no `--resume`, no resume option in the Agent SDK, no resume id in the hub state). The prompt is a context
pack `ctxpack/1` (`ga/ctxpack.py`) built from files in a fixed order (directive head, the session's state file
`.ga/sessions/<s>/state.md`, inbox headers since the session's cursor, then the id-matching lines and the files named in
`refs`), capped at `pack_max_tokens` (rlo pspec estimator, the fixed instructions included). Over the cap, parts are dropped
from the lowest priority and the drops are recorded in the pack head; a directive head over the cap is an error (no turn).
The turn's answer must hold a report/2 and one ```` ```state ```` block: ga checks both, posts the report, writes the state
file and only then moves the cursor; an answer that fails the check is a failed turn (`answer:<why>`), never retried.
Every turn's usage, served model and answer text land in `TurnResult`; each turn is a Telemetry L0 `run.end` record in
`.ga/telemetry/<s>.jsonl`. `runner.context_budget: {soft, hard, mode}` writes the rlo 0.11.0 context-budget/1 hook
(`ga/adapters/budget_hook.py`, shadow by default) into the fresh turn's own settings in the runner's per-session dir only.
`runner.tools` / `runner.system_prompt` narrow the child's fixed cost. Measured: `results/ga29/table.md`
(`examples/ga29_measure.py`).

## `ga supervise`: the same loop on any backend (CMD-GA28, BD-300/302)

    ga supervise --backend <name> --config ga-supervise.json "the task"     # --resume, --prompt-mode as ga gemini
    ga supervise --list-backends            # what loaded (agv first), what did not, each one's fixed overhead
    ga supervise --token-report conv.json   # offline: per host and per backend (fixed overhead, pspec tokens, usage)

A backend is an entry point in the `ga.backends` group (plugin API 1, the rlo.plugins load rules: a plugin that fails to
import, speaks another API, is misnamed or takes a used name is recorded and left out). Built-ins: `agv` (Antigravity,
command `agy`, alias `agy`; slug `gpt-*`, `claude-*` or `gemini-*`, any other family is a config error),
`gemini_cli`, `claude_cli` (`claude -p`), `codex_cli` (`codex exec`), `openai_http` (OpenAI-compatible, local servers
too) and `anthropic_http` (both behind `pip install 'ga-sdk[http]'`). The config form is `ga-supervise/1`:
`{schema, backend, model, options, tools, mcp_servers, budget?, daily?, ...}`; `options` are the backend's (`cli`,
`key_env` for the HTTP ones — the name of the environment variable holding the key; a config holds no key). No budget
or daily cap unless configured (BD-289). A served model other than `model` fails the turn on every backend.

Plan calls are bare where the host allows it (S5): `claude_cli` runs with `--tools "" --strict-mcp-config
--system-prompt <once>`, the HTTP backends send the protocol as the system text and no tools. agv, gemini_cli and
codex_cli have no such switch known offline; `--list-backends` gives their fixed overhead and source (gemini_cli 11,822
input tokens measured by baseline; agv and codex not measured).

The plan form is `ga-plan/1`; `ga-gemini-plan/1` is accepted as its alias, and `ga gemini` (and its `ga-gemini/1` config,
which `ga supervise --config` also runs) still names it, so its verbatim prompts are unchanged. The spec is
`ga/specs/supervisor-plan.pspec`. Every turn follows `prompt_mode` (the first one too, BD-304), and `check_plan` reads
step ids with fullmatch.

## Peer mode: nodes over a code runtime (CMD-GA31, POL-3 structure 4)

Legacy hub mode is the default and unchanged. Peer mode is one switch in ga-config/1 (PROTOCOL 1a: off by default):

    "network": {"mode": "peer", "mailbox": "<git repo>", "theta": 0.3, "edge_budget": {"rpm": 6, "tpm": 20000},
                "refs": {"image.img1.description": {"needs": {"capabilities": ["vision"]}, "input": "img1.png"}},
                "nodes": {"A": {"backends": ["claude_cli"], "task": {"id": "CMD-T1", "goal": "...", "uses": [...],
                                "needs": {"capabilities": ["text"], "tier": "R0"}, "answer"?: "<ref>", "check"?: [argv]},
                                "facts"?: {...}, "catalog"?: [...], "budget"?: {"runs": 6}}, ...}}

    ga node step A                  one step of node A (cron it, or:)
    ga run --every 60 [--steps N]   every node each round (peer mode), or ga tick (hub mode)
    ga usage [--ctx-max N] [--fail] tokmon's ctx / burst / growth alarms from .ga's own L0, no session API

One step: ga mail inbox (the node's own read set) -> the node's rules (a message is the fact 'j sent X'; X is an
observation with evidence or an opinion = a Proposal; only the rules change State: accept, contradicts + uncertain,
dedupe by evidence) -> the `peer_interaction` decision (consult / send / skip; send only on pi >= theta, an open
request, or a contradicts verify flag; the rlo Governor per edge) -> at most one fresh turn on the backend the router
picks (ctxpack with a `3p peer` section, same cap; report/2 + state block + optional ```peer blocks) -> ga check + R6
-> ga mail; L0 `run.end`, `peer.message.sent/received` (facts only, no pi). A task whose answer is already valid in
State is answered without a turn. A node writes only `.ga/nodes/<me>/` (state.json, state.md, cursor, pi.json,
export.json, router.json, run.json, report.md, telemetry.jsonl); a peer form never goes on a hub channel.

Router (NETWORK.md 7): each backend plugin declares a `catalog` (ga/backends/catalog.py); the cheapest entry meeting
`needs` wins, a failed check goes up one tier, three successes come down one; tokens per accepted answer are learnt per
(class, backend, model, tier). A served model other than the chosen one fails the turn; an unknown family is a config
error. A budget stop (`TurnResult.stop = budget_checkpoint`, read from the budget hook log) is a checkpoint in both
modes: the state is kept, the work stays open and continues in a new fresh turn; the same state and branch heads twice
in a row, or a second stop without a state block, stop with needs_judgement; continuations count against the runs
budget. `python tests/mutations_ga31.py` applies the D2 mutations.

## 시험

```sh
python -m unittest discover -s tests
```

## ga judge (CMD-GA30): the verdict steps as a script

`ga judge --report <path|repo@sha:path> --repo <local clone> --base <integration branch> [--mutations spec.json] [--seed N] [--k 1] [--apply]`
runs the mechanical steps of a verdict with no model call: `ga check` on the report head; fetch of its commit and
fast-forward from `--base` (or a dry merge listing the conflicts); an empty venv with `pip install "<dist>[extras] @
git+file://localhost/<private copy>@<sha>"`, the `pip list` of the package and its deps, `pip check`; a fresh clone at the
sha running the repo's test command with `PYTHONDONTWRITEBYTECODE=1` and the network blocked (`unshare -rn` when it works,
plus a socket guard that refuses non-loopback connects); `k` seeded baseline mutations (the seed is printed) that must make
their named tests fail; the report's claims (sha, test counts, `version:<dist>` results) against what was measured.

It prints a `verdict/1` draft head (`dump_wire`), the `needs_judgement` list (deviations, proposals, unmet items, claim
mismatches, surviving or stale mutations, non-ff merges, a missing mutation spec), a DECISION_LOG row and a BASELINE section
13 line. Exit 0: clean success candidate; 1: something for judgement or not success; 2: error; 3: `--apply` refused.
`--apply` fast-forwards the base branch to the sha and pushes, only when the class is success and `needs_judgement` is empty.
Classes: red test → failure/implementation; install failure → failure/implementation; `pip check` failure →
failure/dependency; surviving mutation or no test counts or a bad report head → insufficient/measurement.

Per-repo config: `<repo>/.ga-judge.json` (`dist`, `extras`, `test`, `test_named`, `pinned`, `pip_args`, `pythonpath`, `repo`,
`timeout`; `{python}` is the venv interpreter). Mutation spec: a JSON list of `{id, file, find, replace, tests}`; this repo's
is `.ga-judge.mutations.json`.
