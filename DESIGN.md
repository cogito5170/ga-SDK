# DESIGN — ga-SDK 0.1 설계 (CMD-GA1 rev 4, 2026-10-02)

> 명세는 [`METHOD.md`](METHOD.md)(method-1 rev 4, `5cfa73a`, baseline 소유)다. 이 문서는 **어떻게 짓는가**만 적는다.
> 상태: G1–G9 를 지었다. 시험은 76개이고 Python 3.10–3.13 에서 돈다. §9 검증 결과는 [`examples/verify/RESULTS.md`](examples/verify/RESULTS.md) 에 있다. 명세와 다르게 한 곳과 남긴 것은 맨 아래 §10 에 있다.
> rev 1(`60c9a95`, METHOD `78019c2` 기준) → rev 2: §0 정신(hard/soft) · §3.2 보고 꼴 · §4b(G8) · §5 강도를 반영했다.
> rev 2 → rev 3: §4c Runner(Waker 대신) · 로컬 배치(세션마다 worktree · pre-push 훅) · G9 를 반영했다.
> rev 3 → rev 4: §3.5 교신 `exchange/1` · R1b(어떤 지시에도 속하지 않는 변경은 통합하지 않음) · 프롬프트의 교신 원칙을 반영했다(BD-133).

## 0. 제약과 정신

- Python ≥ 3.10, 핵심은 표준 라이브러리만. (git · pip 는 하위 프로세스로 부른다 — import 하지 않는다.)
- Since CMD-GA20 (BD-206), ga-sdk also *depends on* `rlo-sdk[sensor]` (pinned, `ga/_pins.py`). Core still imports only the standard library; rlo is imported only below `ga.rlo` (§26).
- 저장소 · 세션 id · 브랜치 이름은 코드에 없고 모두 설정에서 온다.
- 1판: **로컬만.** LLM 호출 없음, GitHub · 원격 · 헤드리스 Runner 없음(인터페이스만 그것을 담을 수 있게). 원격 · 네트워크 없이 고리가 돈다.
- **SDK 는 안전만 막고 나머지는 알린다**(METHOD §0). 그래서 검사 결과는 늘 `hard`(막음) · `soft`(알림)를 가진다.
  기계 머리가 틀린 것은 거부하지만, 사람이 읽는 본문 칸이 비어 있는 것은 거부하지 않는다(G1).

## 1. 패키지 모양

import 이름 `ga`, 배포 이름 `ga-sdk`.

```
ga/
  forms/        §3  directive/1 · report/1 · verdict/1 · round/1 · decision/1 · stage/1 · question/1 · exchange/1
                    각 꼴 = dataclass + validate(dict) -> list[Problem] + parse/dump
  records.py    §3.4 기록 저장소(파일) + Markdown 렌더 (G2)
  rules.py      §5  R1–R13 검사기 -> list[Violation(rule, strength, evidence)]  (G4)
  gates.py      §6  게이트 일곱 -> question/1 을 내고 멈춤 (G7)
  prompts.py    §4b 허브 · 작업 세션의 첫 프롬프트 만들기 (G8)
  hub.py        §2  고리 1–6 (받기 → 통합 → 재현 → 판정 → 기록 → 다음) + tick (G3 · G5)
  config.py         허브 설정(JSON): 저장소(로컬 경로) · 브랜치 · 세션(이름 · 머리글자 · 통로) · 소유표 · 시험 명령 · 환경 변수 · 예산 · 규칙 강도 올림
  adapters/
    base.py     §4  Channel · Runner · Vcs · Bundle · Judge  (typing.Protocol) — Ticker 는 `ga tick` 한 번
    mailbox.py      파일 우편함 Channel: `<작업 디렉터리>/.ga/mailbox/<세션>/`, 임시 파일 + os.replace 로 여러 프로세스가 함께 써도 안전
    runner.py   §4c Runner 인터페이스(TurnRequest → TurnResult{ended, session_id, cost}) + 수동 Runner(붙여 넣을 프롬프트를 `.ga/outbox/` 에 쓰고 출력)
    git.py          Vcs (subprocess git): 세션마다 worktree, 원격이 있으면 fetch · push, 없으면 같은 저장소의 브랜치가 곧 통로
    hooks.py        R3: 세션 worktree 마다 pre-push 훅(`extensions.worktreeConfig` + worktree 별 `core.hooksPath`) — 자기 브랜치 밖 · force · 지우기를 막는다
    venv.py         Bundle (a) 경로 방식 · (b) 깨끗한 venv 에 sha 고정 설치
    human.py        Judge 사람 입력 구현(답을 파일에서 읽는다)
  __main__.py       python -m ga  check | tick [--dry-run] | render | bundle | prompt
tests/              unittest (표준 라이브러리)
```

## 2. 형식의 겉모양 (§3)

- 머리는 **JSON**, 글 맨 앞의 ```` ```ga ```` 펜스 블록 하나에 둔다. 그 뒤가 사람 글. GitHub 댓글과 파일 우편함이 같은 꼴이다. (baseline 승인, #12)
- 모든 꼴에 `"schema": "<꼴>/1"` 이 붙는다. 모르는 판 · 빠지거나 틀린 **머리** 필수 칸은 거부한다.
- 지시 id 는 `CMD-[A-Z]+\d+` (`CMD-GA1`, `CMD-K8`, `CMD-G1` 모두 받고 서로 구분된다). 머리글자 중복은 설정 검사에서 잡는다. (baseline 승인, #12)
- **report/1** (METHOD §3.2 rev 2):
  - 머리: `from` · `handled[]{id, rev_seen, status, reason}` · `commits[]`(커밋이 있을 때). 이것만 검사한다.
  - 본문: `Task · Execution · Result · Evidence · Deviation · Blocker · Proposal · Request` — `## <칸>` 제목으로 읽는다. 모두 선택이다.
  - Evidence 의 주장 표시(`verified` · `partially verified` · `not verified` · `assumption` · `blocked`)와 Proposal 의 문제 등급(`blocking` · `current` · `future` · `optional`)은 읽어 내되, 없어도 거부하지 않고 soft 알림만 낸다.
  - 변경 크기(`implementation` · `component` · `interface` · `architecture` · `baseline`)는 머리의 선택 칸 `change_size` 로 받는다. `architecture` 이상이면 게이트 2 로 간다.
  - Deviation 칸에 글이 있으면 허브는 그것을 지시와의 부딪힘 후보로 판정에 올린다(F2).
- 기록은 **정규 JSON**(키 정렬 · `ensure_ascii=False` · 끝 줄바꿈)으로 쓰고 Markdown 은 거기서 렌더한다 → G2 의 "같은 입력이면 같은 바이트".

## 3. 규칙 강도 (§5)

- 각 규칙은 기본 강도를 가진다: hard = R2 · R3 · R4 · R5 · R6 · R9 · R12, soft = R1 · R7 · R8 · R10 · R11 · R13.
- 설정 `rules.raise = ["R7", …]` 으로 soft 를 hard 로 올릴 수 있다. hard 를 soft 로 내리는 설정은 설정 읽기에서 거부한다.
- hard 위반은 그 동작(통합 · push · 지시 보내기)을 하지 않는다. soft 위반은 판정 · 회차 기록에 알림으로만 남는다.

## 3b. 교신과 R1b (§3.5, rev 4)

교신은 정보일 뿐 action 이 아니다. 기계는 이것을 다음처럼 지킨다.
- 허브가 통합하는 커밋은 **보고가 지시 아래에서 주장한 커밋**뿐이다.
  - 보고의 `commits[].sha` 가 그 주장이고, 같은 보고의 `handled` 에 허브가 **그 세션에** 보낸 지시가 하나 이상 있어야 한다.
  - 통합은 주장한 sha 까지만 한다. 그 뒤에 더 쌓인 커밋은 들이지 않고, 몇 개인지 회차 알림에 적는다.
- 아무도 주장하지 않은 브랜치 움직임은 새 것으로 치지 않는다. 보고가 아직 오지 않은 것이기 때문이다. 그래서 tick 은 조용하고, 통합도 하지 않는다.
- 지시 없는 주장(교신만을 근거로 한 변경, 남의 지시 id)은 R1b hard 위반이다. 결과는 통합하지 않음과 판정 "막힘(요구사항)" 이다.
- 세션이 올릴 수 있는 꼴은 `report/1` 과 `exchange/1` 이다. 교신은 회차 알림과 `JudgeContext.exchanges` 에 들어가고, 그 자체로는 아무것도 움직이지 않는다.
- 남의 통로에 쓴 글은 R1 soft 알림을 낸다("보고되지 않은 교신?").

## 4. 고리와 상태

- 허브 상태 파일(`state.json`): 통로마다 마지막으로 읽은 글 id, 저장소마다 마지막 통합 sha.
  tick 은 이것과 비교해 새 것이 없으면 **아무것도 쓰지 않는다**(상태 파일도 안 건드림) → G5 · R9.
- 통합은 ff-only. 실패하면 합치지 않고 판정 `막힘` 으로 넘긴다(R4).
- 통합 전 diff 를 훑어 R2(소유표 — 경로 glob → 세션) · R6(비밀값 패턴)를 본다. 걸리면 통합하지 않는다.
- 판정 `verdict/1`: 숫자(시험 수 · 건너뜀 · 보고 수와 재현 수의 차)는 기계가 채우고, `class` · `next` 는 `Judge` 가 제안한다.
  1판의 `Judge` 는 사람 입력이므로 시험에서는 미리 적어 둔 답 파일을 쓴다.
- 엇갈림(F1): 허브는 지시의 최신 `rev` 를 알고, 보고의 `handled[].rev_seen` 이 그보다 낮으면 `부분 성공(엇갈림)` 으로 가른다.
  (이 설계 초안 rev 1 이 실제로 그랬다 — METHOD `78019c2` 를 보고 썼는데 지시는 이미 rev 2 였다. 시험 사례로 넣는다.)

## 5. Bundle (G3 · F3 · F4)

- (a) 경로 방식: 저장소들을 통합 머리에 체크아웃하고 `PYTHONPATH` 에 나란히 둔 채 설정의 시험 명령을 돈다.
- (b) 설치 방식: 빈 venv 에 `pip install <repo>@<sha>` 를 묶음 전체로 한 번에 넣는다. `ResolutionImpossible` 류 실패를 `고정 충돌` 로 가른다.
- 시험 결과는 unittest/pytest 요약 줄을 읽어 통과 · 실패 · 건너뜀 수로 만든다. **건너뜀 수는 판정에 올린다**(F4).

## 6. 세션 시작 프롬프트 (G8, §4b)

- `ga.prompts.worker_prompt(config, session)` · `hub_prompt(config)` 가 문자열을 낸다. `python -m ga prompt <세션>` 로 찍는다.
- 작업 세션 = 한 줄 머리(세션 이름 · 태그 · 허브 저장소) + **작업 세션 안내 전문** + 통로 주소와 깨우기 방법 + 소유표의 그 세션 줄 + 첫 지시 주소(있으면) + "baseline 이랑 통신 시작해라".
- 허브 = 허브 역할 머리 + **허브 안내 전문** + 통로 · 세션 목록 + 소유표.
- 안내 전문은 코드에 넣지 않는다. 설정이 파일 경로를 가리킨다(baseline `GUIDANCE.md` · `SESSION_GUIDANCE.md`). 시험은 baseline 실제 설정 · 문서 사본(fixture)으로 "두 전문 · 통로 · 소유 줄이 들어간다"를 확인한다.

## 7. 짓는 순서

G1 → G8 → G2 → G4 → G6(우편함 · git) + **G9**(Runner · worktree · pre-push) → G3 → G5 → G7. §9 검증 2(엇갈림)는 G6 와 함께 시험으로 만든다.

## 7b. 로컬 배치 (G9)

- 설정의 저장소는 로컬 경로다. `.ga/worktrees/<세션>/<저장소>` 에 세션 브랜치 worktree, `.ga/worktrees/_hub/<저장소>` 에 통합 브랜치 worktree 를 둔다.
- 원격이 없으면 worktree 들이 ref 를 함께 쓰므로 세션의 커밋이 곧 보고된 브랜치다. 허브는 자기가 기록한 통합 머리와 실제 머리가 다르면 R3 위반(허브 아닌 쪽이 통합 브랜치를 움직임)으로 멈춘다.
- 수동 Runner 는 턴의 끝을 모른다(`ended=None`). 보고가 우편함에 오면 다음 tick 이 연다. 시험에서는 사람 몫을 함수로 흉내 낸다.
- 비용을 아는 Runner 는 턴마다 비용을, 모르는 Runner 는 "알 수 없음" 과 실행 횟수를 상태에 남긴다. R12 는 그 값으로 센다.

## 8. 검증 계획 (§9)

| # | 무엇 | 필요한 것 | 지금 |
|---|---|---|---|
| 1 | stage-2 · 3 · 4 고정 sha 에서 저장소별 시험 수 재현 | Telemetry · Sensor · MS · DC · action · guard · health · rlo-SDK 읽기 권한 | 아직 이 세션에 없음 — 짓는 동안 붙여 본다. 안 되면 `막힘` 으로 보고 |
| 2 | A4/A5 · K1 엇갈림을 파일 우편함으로 흉내 | 없음 | 시험으로 짓는다 |
| 3 | action `2f4791e` + `3995fdb` 섞은 고정에서 (b) 실패 | action 저장소 읽기 + pip 의 GitHub 접근 | 1 과 같음 |

## 9. 위험 · 가정

- (assumption) 시험 환경에서 (b) 의 로컬 git 저장소 설치는 PyPI 접근 없이 돌아야 한다 → `--no-build-isolation` + 시스템 setuptools 로 짓는다. 시험으로 확인한다(baseline 요구). 실제 stage 재현은 네트워크가 필요하다.
- (assumption) 시험 수는 각 저장소의 시험 명령 출력에서 읽는다. 명령은 설정으로 받고, 지금 baseline 이 쓴 명령을 기본값 예시로 둔다.

## 10. 명세와 다르게 한 것 · 남긴 것 (0.1)

- **R7 `done_when`:** §3.1 에서는 필수 칸이지만 §5 R7 은 soft 다. 그래서 없으면 거부하지 않고 R7 soft 알림을 낸다. 설정 `rules.raise: ["R7"]` 로 hard 로 올릴 수 있다.
- **기록의 커밋 · push(§2 5단계):** 하지 않았다. 기록은 `.ga/records/` 에 파일로 쓰고 Markdown 으로 렌더하는 데까지만 한다. 허브 저장소에 커밋하는 일은 사람이나 다음 판의 몫이다.
- **판정 클래스:** 기계가 증명할 수 있는 클래스는 기계가 정한다. 막힘(R2 · R3 · R4 · R6), 실패(시험 실패 · 고정 충돌), 정보 부족(출력을 못 읽음), 부분 성공(엇갈림 · 건너뜀)이 그것이다. Judge 가 제안한 클래스는 더 엄한 쪽으로만 바뀐다.
- **게이트 승인:** 사용자가 질문의 **첫 선택지**를 고르고, 그 decision 을 Judge 의 제안이 `approved_by` 로 인용할 때만 그 게이트를 통과한다. 다른 답이면 계속 멈춘다.
- **원격이 없는 로컬 모드의 R3:** push 가 없으므로 pre-push 훅이 할 일이 없다. 허브가 기록한 것과 다른 통합 브랜치 움직임은 잡는다. 하지만 세션이 남의 세션 브랜치에 직접 커밋하는 것은 아직 잡지 못한다(future).
- **Bundle (b) 의 시험:** 설치된 패키지에 대고, 내보낸 소스 트리에서 돈다. flat 배치 저장소는 현재 디렉터리가 sys.path 앞에 오므로 설치본 대신 소스를 import 할 수 있다(partially verified).
- **Python ≥ 3.12:** venv 에 setuptools 가 없으므로 Bundle 이 setuptools 를 먼저 깐다. 설정의 pip 인자를 쓰고, 오프라인이면 `--find-links` 를 쓴다.
- **§10 "게이트는 설정으로 더 넣을 수만 있다":** 게이트를 더 넣는 설정은 아직 없다. 일곱 개는 고정이고 끌 수 없다.

## 11. 2판 첫째: 로컬 헤드리스 Runner (CMD-GA2)

- `ga/adapters/headless.py`: 한 턴 = `claude -p --output-format json` 한 번이다.
  - 프롬프트는 stdin으로 넘긴다. 다음 턴은 `--resume <session id>`로 이어 간다.
  - 권한은 `--permission-mode` · `--allowedTools` · `--disallowedTools`로 좁히고, 턴마다 `--max-budget-usd`로 비용을 막는다.
  - 깨끗한 환경에서 돈다: 임시 HOME · `CLAUDE_CONFIG_DIR`을 쓰고 `CLAUDE_CODE_*`는 넘기지 않는다.
- `TurnResult`에 `error` · `seconds`를 더했다. 둘 다 기본값이 있어 0.1의 Runner들은 그대로 돈다.
- 허브는 턴마다 수만 남긴다(`state.turns`): 비용 · 시간 · 이어 간 id · 오류. 프롬프트, transcript, 답 글은 남기지 않는다.
- R12는 한도에 이미 닿은 비용도 실행 전에 멈춘다. 다음 턴의 비용은 미리 알 수 없기 때문이다.
- **한계(future):** pre-push 훅은 클라이언트 쪽이라 `git push --no-verify`로 건너뛸 수 있다.
  - 허용 도구에서 `--no-verify` 꼴은 빼 두었지만, `git -C <dir> push … --no-verify`처럼 앞머리가 같은 명령까지 다 막지는 못한다.
  - 확실한 자리는 원격 쪽 훅(bare 저장소의 `pre-receive`)이나 2판의 PreToolUse 훅이다.

## 12. 받는 쪽 R3 · LLM Judge (CMD-GA3)

- **pre-receive(`ga setup`):** 로컬 bare 원격에 R3 정책을 건다. pre-push 와 달리 받는 쪽에서 돌므로 `--no-verify` 로 건너뛸 수 없다.
  - push 한 쪽의 이름은 `GA_SESSION` 이다. 세션 worktree 마다 `remote.<r>.receivepack = GA_SESSION=<세션> git-receive-pack`(worktree 설정)으로 붙고, 허브는 자기 이름으로 push 한다.
  - 정책: 세션은 자기 브랜치만 push 하고, 허브는 통합 브랜치만 push 한다. 이름이 없는 push 는 관리하는 ref 에 쓸 수 없다. force 와 지우기는 거부한다. 거부할 때마다 원격의 `ga-refused.log` 에 한 줄을 남긴다.
  - **한계:** 이름은 push 하는 쪽이 정한다. 셸을 마음대로 쓰는 쪽(`--receive-pack=…`)은 이름을 꾸밀 수 있다. 이 훅이 막는 것은 `--no-verify` 와 실수까지다. 악의를 막으려면 원격이 사람마다 신원을 확인해야 한다(GitHub 브랜치 보호 등).
- **LLM Judge(`ga/adapters/llm_judge.py`):** `claude -p` 한 번이 판정 한 번이다.
  - 도구는 쓰지 않는다(`--tools ""`). 세션을 저장하지 않고, 깨끗한 환경 · 임시 HOME 에서 돈다. 문맥은 stdin 의 JSON 이다.
  - 답이 `verdict/1` 검사를 통과하지 못하거나, 호출이 실패하거나, 예산이 다하면 기계의 클래스를 그대로 두고 `ask_user` 로 물러선다. 초안 지시는 `directive/1` 을 통과할 때만 남긴다.
  - 허브는 기계의 클래스를 바닥으로 지키고(더 엄하게만), 게이트에서 멈추며, 판정 호출의 수(비용 · 시간)를 남긴다.
  - `--json-schema` 는 스텁으로만 시험했다. 실제 CLI 에서 `--tools ""` 와 함께 쓸 수 있는지 확인하지 않았기 때문에 기본은 끈다.

## 13. 엇갈림은 사실 · 헤드리스 턴의 가드 (CMD-GA4)

- **엇갈림(METHOD rev 6, BD-138):** 기계는 엇갈림을 `rev_seen` 과 대체 여부로 증명하고, 증거 알림과 subclass `crossed` 를 붙인다. 클래스의 바닥은 아니다. 클래스는 Judge 나 사람이 정한다.
- **가드(`ga/adapters/bash_guard.py`):** 헤드리스 Runner 가 자기 임시 디렉터리의 `--settings` 로 PreToolUse 훅을 건다. 실제 `~/.claude` 는 쓰지 않는다. 표준 라이브러리만 쓰고 혼자 돈다.
  - 막는 것: `--no-verify` · `--receive-pack`/`--exec`/`--upload-pack` · `git -c`/`--config-env`/`git config` · `hooksPath` · `GA_SESSION` · 명령 자리의 `GIT_*=`/`env` · `--git-dir`/`--work-tree` · `.git`/hooks 로의 파일 쓰기 · 셸 우회(`$()` · 역따옴표 · `${}` · `$VAR` · `eval` · `base64` · `sh -c` · `python -c` · `source` …).
  - 읽지 못한 입력은 막는다. 기록에는 규칙 이름만 남는다.
  - 겹 방어의 순서: 가드(턴 안) → pre-push(보내는 쪽) → pre-receive(받는 쪽).
  - **남은 한계:** 문자열 규칙이다. 그래서 셸 우회를 통째로 금지하는 쪽(닫는 쪽)을 골랐다. 사람이 직접 셸을 쓰는 경우(수동 Runner)는 가드 밖이다. 거기서는 pre-receive 와 원격의 신원 확인이 지킨다.

## 14. R3 를 구조로 (CMD-GA5)

- **GA4 의 잘못:** 문자열 가드를 "닫는 쪽" 이라고 보고했지만 스크립트 실행 길이 열려 있었다. baseline 이 그 밖에도 따옴표로 나누기와 옵션 줄임을 찾았다.
  - 로컬 원격에서는 받는 쪽 명령도 push 하는 쪽이 고른다(`--receive-pack`). 그래서 pre-receive 도 보장이 되지 못한다.
- **pull, 독립 clone (`isolation: "clone"`, 기본):** 세션은 push 하지 않는다. 허브가 `git fetch <세션 clone> +refs/heads/<자기 브랜치>:refs/ga/sessions/<세션>/<브랜치>` 로 가져온다.
  - R1b(지시 아래의 주장)와 R2 · R4 · R6 은 그대로 통합 전에 본다.
- **쓰기 샌드박스(`ga/adapters/sandbox.py`):** 바깥 user/mount namespace 에서 보호할 경로를 읽기 전용으로 bind 하고, 자기 자리만 읽기·쓰기로 다시 bind 한다. 명령은 한 겹 안쪽 user namespace 에서 돈다. 그 안에서는 마운트가 잠겨 있어(kernel MNT_LOCK_*) 다시 마운트하거나 떼어 낼 수 없다.
  - 허브가 턴마다 무엇을 보호하고 어디에 쓸 수 있는지 정한다(`Hub.sandbox_paths`). 헤드리스 Runner 는 `sandbox: auto | require | off` 로 감싼다.
  - Claude 홈은 세션마다 따로 둔다. 턴 기록에는 `sandboxed` 가 남는다.
- **문자열 가드:** 첫 겹으로 남긴다. 따옴표를 뺀 꼴도 보고, 옵션 줄임과 스크립트 실행도 더했다. **보장으로 세지 않는다.**
- 모드별로 무엇이 지켜지고 무엇이 안 지켜지는지는 README 의 표에 있다.

## 15. Agent SDK Runner · 턴 진단 라벨 (CMD-GA6)

- `ga/adapters/agent_sdk.py`: 한 턴 = `claude_agent_sdk.query()` 한 번이다. 끝은 `ResultMessage` 로 안다(session_id · total_cost_usd · is_error).
  - SDK 는 선택 의존(`[agent]`)이다. 핵심 시험은 가짜 SDK(`fake_sdk`)로 돌므로 SDK 없이도 초록이다.
- 보장은 SDK 가 띄우는 CLI 자리(`cli_path`)에서 지킨다. SDK 는 이 프로세스의 환경을 통째로 넘기기 때문이다(`CLAUDECODE` 만 뺀다).
  - 세션마다 래퍼를 둔다: `env -i` + 허락한 변수 + (샌드박스) + 실제 CLI.
  - 래퍼는 Runner 의 `_wrappers/` 에 있다. 이곳은 샌드박스가 보호하고, 턴마다 다시 쓴다.
- 턴 진단 라벨 `turns[].diag = {committed, posts, reports_ok, claims_known}` (GA5 rev 2 의 F8 꼴을 막는다).
  - 턴 시작 때 세션 머리와 지시 글 id 를 기록한다. 끝을 아는 Runner 는 턴 직후에, 그 밖의 Runner 는 세션이 글을 올린 tick 에서 계산한다.

## 16. GitHub 이슈 Channel · 원격 세션 Runner (CMD-GA8)

- `ga/adapters/github.py`: `GitHubIssueChannel` 은 이슈 댓글 API 만 쓴다(목록 · 만들기). 글 id 는 댓글 id 를 20 자리로 채운 것이라 우편함처럼 올린 순서로 정렬된다.
  - 한 계정이 허브와 세션을 다 쓸 수 있으므로 작성자를 댓글의 마지막 `<!-- ga-author: X -->` 줄로 나른다. 그 줄 뒤에 플랫폼이 꼬리말을 붙여도 된다. 이 줄은 주장이다(로그인도 같은 계정이면 마찬가지다).
  - 시험은 가짜 HTTP 서버(`tests/test_github_remote.py`)로 모든 길을 돈다: 쓰기 · 읽기 · 페이지 넘김 · 페이지 중간의 속도 제한 · 401/404/422/500 · 닿지 않음 · 토큰 없음.
- `RemoteSessionRunner(send)`: 같은 Runner 인터페이스. `send(request) -> {"session_id"}` 는 원격 세션을 만들거나 깨울 힘이 있는 쪽이 준다. 라이브러리 안에서 부를 공개 API 가 없기 때문이다(막힘, 대안 = 콜백).
- `isolation: "remote"`: 허브는 세션 checkout 을 만들지 않고, 원격을 fetch 해 세션 브랜치를 읽는다. `Repo.push: false` 면 통합 브랜치를 로컬에서만 ff 한다. 세션 쪽 R3 는 플랫폼의 몫이다(README 표).
- 실제 한 바퀴의 중계 스크립트는 `examples/verify/ga8_real.py` 다(init · send · tick). 허브는 로컬에서 돌고, 에이전트가 글과 세션을 MCP 도구로 나른다.

- 기계 바닥 하나를 더했다(CMD-GA9, METHOD rev 8 §3.3, BD-144): 그 회차의 세션 글이 모두 형식에서 거절되고(report/1 · exchange/1 이 아니거나 세션이 쓸 수 없는 꼴) 받은 보고 0 · 통합 0 이면 `insufficient`(원인 requirement). 근거 notes 에 거절 수가 남는다. 받은 보고가 하나라도 있으면 걸리지 않는다.

## 17. 실사용 1 차의 결함 고침 (CMD-GA11, METHOD rev 9)

- GA10 실사용에서 ga 판정은 baseline 과 클래스 1/3 · 원인 0/3 이었다. 어긋남은 LLM Judge 가 아니라 기계 규칙 · 턴 프롬프트 · 설정에서 왔다.
- `expected_skipped`(정확한 수 + 까닭) · `not_install_checked` 바닥 · R4 비ff 의 자동 rev+1(`Hub._merge_again`, 게이트 없음, 그 회차 Judge 의 같은 id 지시는 보내지 않는다).
- `VenvBundle.import_check`: (b) 의 시험은 checkout 에서 돌아 소스 사본이 설치본을 가린다. 그래서 설치된 배포가 낸 최상위 패키지마다 소스의 모듈(`__main__` 빼고, `__init__.py` 가 이어진 곳만)을 빈 디렉터리에서 import 한다.
- `report_template`: 턴 프롬프트에 report/1 머리 틀. `ga_dir` 는 Hub · GitVcs · CLI 에서 `resolve()` 한다.
- 시험 세계(`tests/world.py`)는 Bundle (b) 를 돌릴 때만 포장된 저장소를 만든다(`packaged`). 경로만 돌리는 세계가 포장돼 있으면 rev 9 바닥에 걸리기 때문이다.

## 18. 바깥 판정 · 빈 초안 · 설치 환경 (CMD-GA12, METHOD rev 10)

- `review/1`(id `RV-n`, by · repo · sha · class · cause · why, round · amends 는 허브가 채움)은 append-only 기록이다(`records/reviews/`). `Hub.review` · `ga review`.
  - 회차는 다시 쓰지 않는다. 고침은 리뷰 쪽 `amends` 에 있고, `ROUNDS.md` 가 그 회차 밑에 그린다.
  - 열린 리뷰는 다음 tick 을 깨운다. 그 회차에서 `used` 가 된다.
- `Hub._fill_drafts`: Judge 가 refine · verify 에 초안을 비우면 세션마다 하나씩 만든다. 대상은 바깥 판정의 sha 를 통합한 지시(`state.integrated_by`), 보고가 다룬 지시, 보고가 아무 지시도 다루지 않으면 그 세션의 열린 지시 순이다.
  - 각 세션 초안에는 자기 `R1b` note 만 넣는다. R4 로 이미 rev+1 이 나간 지시는 건너뛴다. 게이트가 생기면 그 뒤 초안은 보내지 않는다.
- `VenvBundle.build_tools` → `BundleResult.tools` → 근거 note.
- `examples/verify/ga12_replay.py`: GA10–11 운영자 개입 6 번을 고친 ga 로 다시 만든다. 3 번은 필요 없고, 2 번은 Judge 의 선택에 달렸고, 1 번(설정)은 남는다.

## 19. ask_user 의 근거 · 바깥 판정 뒤 wait (CMD-GA13, METHOD rev 11)

- `gates.ask_basis(proposal)`: 제안의 `gate` 가 1..7 정수일 때만 그 번호. `detect` 는 그때만 Judge 의 ask_user 를 그 번호의 게이트로 만든다(예전에는 늘 게이트 5).
  - 허브는 근거 없는 ask_user 를 soft 알림으로 남기고 회차의 다음을 wait 로 적는다(R10 과 맞물림). Judge 의 verdict 파일에는 Judge 가 낸 ask_user 가 그대로 남는다.
  - LLM Judge 의 프롬프트에 일곱 게이트와 `"gate"` 칸을 넣었다. 응답의 `gate` 는 1..7 일 때만 제안에 실린다.
- 바깥 판정 뒤 wait: 그 회차에 쓰인 리뷰마다, 그 sha 를 통합한 세션에 열린 지시가 없으면(이 회차에 보낸 지시도 열린 지시다) 알림.
- 재생(`examples/verify/ga13_replay.json`): 개입 6 번 가운데 필요 없음 4 · Judge 에 달림 1 · 남음 1.

## 20. Judge 실패 회차 (CMD-GA14, METHOD rev 12)

- `LLMJudge.fallback` 이 `judge_failed` 를 싣는다. 허브는 그것으로 실패를 센다(`state.judge_failures`, 연속 수).
- 실패 1: 기계 클래스로 판정(대체 판정의 클래스). `state.judge_retry = {round, pending}` 을 남긴다. 다음 tick 은 새것이 없어도 회차를 돌며 그 증거로 Judge 를 다시 부른다. 그 회차의 보고 게이트는 다시 묻지 않는다. 기록은 새 회차(append-only)이고 알림 "retry of round n" 이 붙는다.
- 실패 2(연속) 또는 다시 부를 예산이 없음(`budget.judge_runs`): 게이트 6. 재시도는 지운다.
- 성공 한 번이면 `judge_failures = 0`, 재시도는 지운다.

## 21. 권한 사전 확인 · 보내지 않음 · 수동 강등 (CMD-GA15, METHOD rev 13 §4c)

- `decision/1.scope`(runner · model · sandbox · budget · measurement_calls)는 선택 칸이다. `Hub.permit` · `ga permit` 이 by user 로 남긴다. 설정 `runner.permission` 이 그 id 를 가리킨다(형식 검사 `BD-n`, Runner 인자로는 넘기지 않음).
- `Hub._permission_gap`: Runner 의 kind 가 `RUNNER_KINDS` 이면 결정이 있는지, user 인지, scope 가 있는지 본다. 그리고 runner · model · sandbox 가 같은지, scope 예산을 넘지 않았는지 본다. 하나라도 어긋나면 통로에 쓰기 전에 `not_sent` 와 게이트 6.
- 거부: Runner 가 `refused:…` 를 돌려준다. 헤드리스 · Agent SDK 는 `PermissionError`, 원격은 콜백의 `PermissionError` 나 `{"refused": …}` 다. 허브는 지시를 `not_sent` 로 두고, 턴 기록에 `sent: false` 를 남기고, 실행 수에 넣지 않고, 게이트 6 을 낸다. 같은 id · rev 는 고친 뒤 다시 보낼 수 있다.
- 질문: 혼자 부른 `send` 는 질문(`Q-<n>-p<k>`)을 스스로 쓴다. tick 안에서는 그 회차의 질문으로 쓴다. 게이트에 `options` · `directive` 를 실어 보낸다.
- `answer` 가 그 질문에 `수동으로 강등한다` 를 받으면 `_downgrade` 한다(같은 지시 · ManualRunner · 기록). 이 질문들은 Judge 회차를 부르지 않는다(`processed`).
- 시험 세계(`World.permit_runner`)는 모형 턴 Runner 에 그것과 같은 범위의 허락을 남긴다. 사람이 첫 send 전에 하는 일과 같다.

## 22. Judge 호출의 허락 (CMD-GA16, METHOD rev 14 §4c 6)

- `LLMJudge.calls_model = True`. 허브는 그런 Judge 를 부르기 전에 `_judge_permission_gap` 을 본다. 결정이 user 의 것이고, `scope.measurement_calls` 가 true 이고, (Judge 만 허락이면) 모형이 같고, `budget.judge_runs` 안이어야 한다.
- 어긋나면 `_machine_proposal`(기계 클래스 그대로)로 판정하고, note "Judge 허락 없음" 을 남긴다. 게이트 6(`JUDGE_PERMIT_OPTIONS`)은 같은 꼴의 질문이 열려 있지 않을 때만 낸다. `judge_failures` 는 그대로 두고, 이 회차가 쓴 rev 12 재시도는 지운다. 그 질문의 답은 Judge 회차를 부르지 않는다.
- `decision/1.scope.runner` 에 `manual` 을 더했다(Judge 만 허락). `ga permit --judge-only` 가 그것을 남긴다.
- 시험 세계: `World.permit_judge(judge)`.

## 23. 작업 턴의 운영자 가드 (CMD-GA17, METHOD rev 15 §4c 7)

- `HeadlessRunner(guards=...)`(Agent SDK 는 같은 layout 을 씀): `settings_file` 이 ga 의 bash_guard 다음에 운영자 가드를 넣는다. ga 가드를 끄면(`guard=False`) 운영자 가드만 들어간다. `fill` 이 `{session}` · `{home}` 을 채우고, `guard_records` 가 (이름, 기록 파일)을 낸다.
- `Hub._guard_gap`: 명령의 첫 낱말(프로그램)이 경로면 실행 가능한 파일인지, 아니면 PATH 에 있는지 본다. 없으면 통로에 쓰기 전에 `not_sent` · 게이트 6(`GUARD_OPTIONS`).
- `Hub._run_turn`: 턴 전 기록 파일의 줄 수를 재고, 턴 뒤 늘어난 줄만 `guard_summary` 로 센다(ga 가드 로그 꼴과 rlo `--record` 꼴). tick 은 보고가 들어온 세션의 턴 가운데 거부가 있는 것을 근거 note 로 한 번 남긴다(`guards_noted`).
- 설정 검사: guards 는 headless · agent_sdk 만, 항목은 문자열 칸(command 필수)만.

## 24. 꼴 판 2 (CMD-GA18, METHOD rev 16 §3.6)

- `ga/forms/kinds.py`: `directive/2` · `report/2`(= report/1 칸 + items · results · blockers · deviations · proposals) · `notify/1`. 교차 검사는 셋이다. rev 1 은 항목 필수이고 changes 가 없어야 한다. rev > 1 은 changes 필수(hard)다. add · edit 는 text 필수다. 항목 id 는 겹치지 않는다. `deprecated(schema)` 는 판 1 의 soft 알림이다.
- `apply_changes(prev, doc)`: 앞 판의 항목에 changes 를 얹어 전체 판을 낸다. 허브 `send` 는 전체 판을 `doc` 에, 보낸 그대로를 `sent` 에, 계산의 바탕을 `base` 에 둔다(보내지 않은 판을 다시 보낼 때 같은 바탕을 쓴다). 통로에는 보낸 그대로가 간다. 턴 프롬프트는 전체 판을 보인다.
- 허브가 스스로 만드는 rev+1(R4 의 합치기, 빈 초안 메우기)은 `_next_rev` 를 거친다. 원래가 directive/2 면 changes 로 만든다(항목 더하기, R4 는 끝난 기준을 바꿈).
- 받기: report/2 의 items 가 다룬 directive/2 의 done_when 을 빠뜨리면 그 글을 받지 않는다(R7 hard, rev 8 의 "모두 거절" 바닥과 맞물림). 바닥(blocked · 부분 성공)은 `_reproduce` 에, 게이트 6 은 `gates.detect` 에 있다.
- 턴 프롬프트의 보고 틀은 report/2 다(directive/2 면 items 를 미리 채움). 세션 첫 프롬프트에 꼴 안내를 넣었다. CLI 는 `ga check`(판 1 이면 deprecated 알림), `ga post`(report/2 · report/1), `ga notify` 다.

## 25. na rule, guard state, prompt config errors (CMD-GA19, METHOD rev 17)

- `_reproduce`: for a report/2 with `done`, an item with state `na` and a non-empty `evidence` list leaves the partial-success floor (`_has_reason`), and the round notes name it. An `na` with no evidence stays "not met". Blank evidence strings never reach this point: the forms refuse them (hard), so that post is refused and the rev 8 floor applies. A reasoned `na` excuses only itself; an `unmet` or `blocked` item beside it still floors.
- `guard_summary`: `{"kind": "state", "labels": {…}}` lines fill `state` (GR1 request 1, so ga_rlo's Sensor state can live in ga's own turn evidence). They are not counted as allow or deny. Keys and string values must match `STATE_LABEL`; numbers, bools and null pass. A rejected entry is dropped, and the line counts once in `errors`. The field appears only when some state was kept, so the rev 15 records are unchanged.
- `prompts._guidance`: a missing or empty `hub.guidance` / `hub.session_guidance`, or an unreadable file, raises `FormError` with the key's path. `ga prompt` turns that (and an unknown session) into exit 2 on stderr (GR1 request 2).

## 26. One ga: the rlo pin and the `ga.rlo` seam (CMD-GA20, GA_UNIFIED U2, BD-206)

- **Pins.** `ga/_pins.py` `PINS` is the source: `rlo-sdk[sensor] @ git+https://github.com/cogito5170/rlo-SDK@…` (rlo 0.6.0 `3323f88` in GA20; K12 rev 2's `250a88e`, rlo 0.7.0, in GA21; K12 rev 3's `3d2e7d0`, rlo 0.7.0, after GA21 rev 3 (BD-247); `c491e96`, rlo 0.8.2 (K13's deadlock fix, K14's bounded window and incremental feed), since CMD-GA25 rev 2 (BD-268, BD-278), GA21). `pyproject.toml` `[project] dependencies` must match it by meaning (name, extras, URL, marker; spacing and name case do not count). The URL text is part of the pin: pip refuses two spellings of a URL for one distribution, so anything else that pins rlo-sdk next to ga-sdk must use the same text. `tests/test_unified.py` also checks, where ga-sdk is installed, the Requires-Dist, rlo-sdk's version and the commit pip fetched (`direct_url.json`).
- **Lazy import.** No ga module outside `ga.rlo.*` imports rlo. The test puts a fake `rlo` first on the path, imports every ga module (except GR's below the seam), runs `ga --help`, `ga check` and `ga rlo`, and asserts that no `rlo` module was loaded.
- **Seam.** `ga/rlo/__init__.py` holds only a docstring and `__version__`. `ga rlo ...` is caught in `main` before argparse (`_rlo_argv` skips `--config` / `--ga-dir` and their values). It is still listed in `ga --help`. `cmd_rlo` imports `ga.rlo.cli` and calls `main(argv)` with the rest. Only that import is guarded: `ModuleNotFoundError` gives exit 2 naming `e.name`, either "is not there yet" (the module itself) or "is missing (ga.rlo.cli needs it)" (one of its imports).
- **Ownership.** `ga/rlo/*` and `tests/test_rlo/*` are GR's (GA_UNIFIED U-P2, BD-218); GA does not edit them. Not `tests/rlo/`: with `unittest discover -s tests` a `tests/rlo/__init__.py` is a top-level package `rlo` and shadows rlo-sdk. GA owns `cmd_rlo`, `_rlo_argv` and `ga/_pins.py`. A change GR needs on GA's side goes to GA as a `요청:`, and the reverse too.

## 27. `ga gemini` (CMD-GA21 rev 3, BD-221, BD-222, BD-232)

- **`ga/adapters/gemini_cli.py`** (stdlib), rev 2 on the 0.62.0 shapes baseline read from the source (baseline#12 5970108172):
  - `GeminiCLI.run_turn` runs one process per turn (`subprocess.run`, stdin `DEVNULL`, a timeout) in a fixed working directory (the CLI keeps sessions per project dir; `--resume` takes the session UUID).
  - `parse_stream`: `init` gives the session id and the *asked* model; `message` the assistant text; `error` events count as `cli_errors` (severity error: max session turns) or warnings (loop detected); `result` gives status, `error{type, message}`, usage and the served models (the keys of `stats.models`, nothing else).
  - Order of judgement: no `result` → `no_result` (crashed turn); `quota_of` → `GeminiRateLimited(kind, hint_s, via)`; `status` not success → `result_<error type>`; a severity-error event → `cli_error`; served empty → `served_model_unknown`; any served model other than the asked one → `served_model_mismatch`. The exit code is never read (0.62 exits with the error's status, 429 → 173).
  - `quota_of`: first by `error.type` (`RetryableQuotaError` minute, `TerminalQuotaError` day; a per-day quota in the message makes it day); then, only for another class, by a 429 / quota sentence in the text (`via: text`). A "retry in N s" in the message is the hint for a minute wait.
  - `write_settings`: reads the system settings in force (`GEMINI_CLI_SYSTEM_SETTINGS_PATH` at construction, else the platform default), sets `general.maxAttempts = 1`, writes `<state dir>/gemini-cli-settings.json` and points `GEMINI_CLI_SYSTEM_SETTINGS_PATH` at it. An unreadable system file is an error, never dropped silently. Baseline confirmed from the 0.62 source that the variable is honoured and system settings win the merge — but `retryWithBackoff` takes the model policy's `maxAttempts` first, and the preview chain gives `gemini-3-flash-preview` 10, so for that model the setting does not bound the CLI's in-turn retries (BD-232).
  - `run_turn(on_wait, wait_every_s)`: `Popen` + `communicate(timeout=…)` in a loop (a retried `communicate` loses no output); every `wait_every_s` the process still runs, `on_wait(seconds)`; the turn's own timeout still kills it.
  - `CLAUDE_CODE_*` variables are dropped from the CLI's environment.
- **`ga/adapters/mcp_stdio.py`**: `StdioClient` does initialize, notifications/initialized, then tools/call. It returns `structuredContent` or the text parts; `isError` raises `MCPToolError`. One server process per client, closed when the task ends.
- **`ga/gemini.py`**:
  - `ga-gemini/1` config; unknown fields are errors, so there is no fallback field to set. `ga-gemini-plan/1` check: at most 16 steps, label ids, `after` only to earlier ids, tools only from the table, `next` at most one object.
  - `Supervisor` builds K12 `Scheduler` and `Governor` per run. The step table is `{"gemini": "model", <tool>: "tool"}`; step ids are `T<task>.m<n>` and `T<task>.r<n>.<plan id>`.
  - The model step's provider runs the turn, checks the plan, and adds its steps to the running scheduler (`_extend`). On `GeminiRateLimited` it records the wait's source (hint · window · daily reset); for the daily quota it marks today spent (`DayCount.exhaust`) and sets the error body's wait to the reset, then re-raises, so the scheduler parks the step; any other error fails the step.
  - The scheduler calls `sleep` only when nothing can run, i.e. when a model step is parked. `_sleep_hook` then prints the block once per park, writes `parked[id] = [resume_at, since, source]` to the state file, and sleeps.
  - `status()` is K12 rev 2's `Scheduler.status()` {resumes_in_s, done, running, parked, next}, plus `eta_source` (hint · window · daily reset) and `left_today`. `resumes_in_s` None while parked means no window opens.
  - S7 (rev 3): `_turn_wait(sid)` prints `turn <id> running N s; the CLI may be retrying a quota error inside the turn · done · running · parked` and logs `turn_wait`, once per `turn_status_s` (default 20). The Governor's budget is the config's (`DEFAULT_BUDGET` `{"rpm": 5}` when absent).
  - S8 (rev 3): `max_parallel` goes to the Scheduler when its signature takes it (K12 rev 3); otherwise `max_parallel_pending` is logged. Tool steps may then run on the Scheduler's threads: state, log and result writes take `Supervisor._lock`, and each MCP server's client takes its own lock (one request at a time per server, as answers come from one queue). Since the pin moved to K12 rev 3 (`3d2e7d0`), `max_parallel` is handed over (default 4 in the Scheduler when absent).
  - The daily quota (S6): `DayCount` (`day.json`: requests used, the next reset) and `daily_governor`, a subclass of rlo's Governor whose `wait_s` adds "wait for the reset" when none are left and whose `try_acquire` counts a request. `next_reset` uses `zoneinfo`. At the reset the parked step is dispatched once: the probe (`log` event `probe`). A probe that hits the quota again parks to the next reset. The block is shown again for each new park (`_shown` resets on dispatch).
  - Two state files: ga's `state.json` (session id, the plan's steps with their tools, args and prompts, done, parked) and K12's `scheduler-<task>.json` (`Scheduler(state=…)`, saved on every ledger row: step states, JSON results, Governor windows, VERIFY windows). `--resume` adds every step of the task again, done ones too, and K12 reloads the rest, the remaining wait included.
  - ga saves `state.json` before the scheduler can write a row for new steps, so K12's file never names a step ga's file lacks.
  - A tool step that was running when the process died runs again (at least once).
- Tests: `tests/test_gemini.py` with `fake_gemini.py` (a scripted CLI), `fake_mcp.py` and `gemini_tools.py`. The supervisor tests need rlo ≥ 0.7.0 and skip without it; an installed ga brings it.

## 28. `ga mail` (CMD-GA22, BD-235)

- **`ga/mailbox.py`**, standard library plus git. `Mailbox(repo, remote, retries=20)`.
  - `tip()` fetches `refs/heads/ga-mailbox` into a ref of its own (`refs/ga-mailbox/fetch-<uuid>`, deleted after) with `--no-write-fetch-head --refmap=`. Concurrent sends in one clone share neither `FETCH_HEAD` nor the remote-tracking ref; without `--refmap=` the 20-sender test failed on `cannot lock ref`.
  - `send` validates (`parse_text`, `validate`, hard problems refuse) and checks `SECRET_PATTERNS`, then: `hash-object -w`, `read-tree <tip>` (or `--empty`) into a temporary `GIT_INDEX_FILE`, `update-index --add`, `write-tree`, `commit-tree -p <tip>`, `push <commit>:refs/heads/ga-mailbox` (never forced). A rejection loops back to `tip()`. The sleep is `uniform(0.2, 1) × min(3, 0.1 × 2^attempt)`. A name already on the tip gets a new time.
  - The file name is `to/<recipient>/<YYYYmmddTHHMMSS.ffffffZ>-<sender>-<form id>.md`. The form id is a directive's `id`, else a report's first handled id, else the schema. Senders match `[A-Za-z0-9_.]` (no `-`), so `parse_path` splits one way; recipients match `[A-Za-z0-9_.-]`.
  - `unread(name)` yields `Message`s (sender, form, schema, validation problems, a from/file-name mismatch) oldest first; the caller marks each read after showing it. The read set is a file of names under the git common dir.
  - `scan()` reads every message once. Unread is per the local read set (`basis: cursor`) or else the messages after the name's last send (`basis: since_last_send`). The oldest unanswered report is a `report/*` to the name with no later message from the name to its sender.
  - `guard_report(lines, sender, directive, rev, items)`: `hub.guard_summary` over the lines, wrapped as `report/2` with handled `paused`, a `permission` blocker and the items `blocked`. GR7's own event file is not written yet; these are the two record shapes ga already reads (assumption).
- The read set is in the git dir, not `.ga/mailbox/` in the work tree as the directive says: a worker that runs `git add -A` would commit a `.ga/` folder. It is still local and never pushed.
- **CLI** `ga mail send|read|scan` (`ga/__main__.py` `cmd_mail`); refusals are exit 2 with `ga mail: …` on stderr.
- **Tests** (`tests/test_mailbox.py`): a bare remote with clones as sessions. They cover:
  - send, read and the read set; untouched trees and branches; no edit;
  - every refusal; a sender mismatch;
  - 20 concurrent sends (10 per side) with no loss;
  - a remote that always rejects (4 pushes, 3 sleeps under the cap);
  - marking read only after printing; scan; the guard event;
  - an end-to-end run through `bash -c` with a minimal environment.

## 29. Google secrets in R6 (CMD-GA24, BD-254)

- `rules.SECRET_PATTERNS` adds `AIza…{35}` (Google / Gemini API key), `ya29.…` (OAuth access token), `1//…` (OAuth refresh token), `GOCSPX-…` (OAuth client secret) and the JSON field form `"access_token" | "refresh_token" | "client_secret": "<16+>"`.
- They reach every user of the list: R6 (hub: outgoing directives and integration diffs; `ga check`; `ga post`) and `ga mail` (refused on send, flagged on read). `tests/test_secrets.py` has a test per place, fakes assembled at runtime, and a no-false-positive check over every tracked text file and a form that only names `GEMINI_API_KEY`.
- That repository-wide check found one old hit: GA15's `tests/test_github_remote.py` held a fake token as a literal (`TOKEN = "fake-token-…"`), caught by the existing `token = '…'` pattern. It is now assembled at runtime.

## 30. `ga gemini --host agy` (CMD-GA23, BD-234/255)

- **`ga/adapters/agy_cli.py`** (stdlib). Facts carry baseline's labels (baseline#12 5971559909): V verified, A assumption, U unknown.
  - `AgyCLI.run_turn` runs `agy -p … --output-format stream-json --model <pin>` in the workspace, stdin closed, with the in-turn callback of GA21.
  - `parse_output` reads one JSON value or JSONL, at any depth:
    - `model` (A: a string, or an object with `display_name` / `name` / `id`) gives the served model;
    - `denied_actions` (V; entry shape U) gives labels;
    - text comes from deltas, or else the last whole answer.
  - Plain lines: `AGY_ERROR` (V); quota words with it (A) mean a quota stop; "AI credits" (V) means credits.
  - Order of judgement: credits → `AgyQuota("credits")`; quota → `AgyQuota("quota")`; exit ≠ 0 or `AGY_ERROR` → `agy_error`; no served model → `served_model_unknown`; another model → `served_model_mismatch`.
  - `resumes = False` (U: `--resume`), so the supervisor sends the protocol and task in every prompt.
  - `usage()` runs `agy -p /usage` (V: no quota spent). `parse_usage` is lenient (U): per line, a family (gemini, or claude_gpt), a percent (`used` is turned into remaining), the window (`weekly` or `5-hour`), and a reset `YYYY-MM-DD HH:MM[:SS]` with a zone abbreviation from a small table, or an ISO offset. An unknown zone gives no reset.
- **`ga/gemini.py`:**
  - The config adds `host` (`gemini_cli` | `agy`; `ga gemini --host` overrides it) and `agy` {model, cli, usage_floor_pct, window, reset_fallback_s, usage_every_s}.
  - `cfg.active_model` is the one model of the host. The state, Governor and logs use `Supervisor.model`.
  - `AgyQuota` holds the family's reading. `check()` gives the wait before a model step; it probes when the reading is older than `usage_every_s` or a held reset has come. Under the floor it holds until agy's reset (or `reset_fallback_s` when none is readable).
  - `spent()` is called after a quota or credits stop: it probes and holds until the reset.
  - `agy_governor` is rlo's Governor with `try_acquire` gated by `check()` and `wait_s` raised to the hold, so the scheduler sleeps exactly until the reset. The error body carries the same wait, so after a restart the K12 state keeps it.
  - The status block for agy shows family, window, share left, floor, reset and "one probe then". The probe at the reset is logged like the daily one.
  - `denied` is printed and logged (count, labels).
- An unreadable `/usage` does not block: the reading is logged as unknown, and a quota stop still holds for the fallback.
- **Tests:** `tests/test_agy.py` on `tests/fake_agy.py` (assumed shapes), 14 tests. 14/14 GA23 mutations killed; the GA21 set re-run gives 37/38, the one equivalent survivor as before.

