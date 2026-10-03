# DESIGN — ga-SDK 0.1 설계 (CMD-GA1 rev 4, 2026-10-02)

> 명세는 [`METHOD.md`](METHOD.md)(method-1 rev 4, `5cfa73a`, baseline 소유)다. 이 문서는 **어떻게 짓는가**만 적는다.
> 상태: G1–G9 를 지었다. 시험은 76개이고 Python 3.10–3.13 에서 돈다. §9 검증 결과는 [`examples/verify/RESULTS.md`](examples/verify/RESULTS.md) 에 있다. 명세와 다르게 한 곳과 남긴 것은 맨 아래 §10 에 있다.
> rev 1(`60c9a95`, METHOD `78019c2` 기준) → rev 2: §0 정신(hard/soft) · §3.2 보고 꼴 · §4b(G8) · §5 강도를 반영했다.
> rev 2 → rev 3: §4c Runner(Waker 대신) · 로컬 배치(세션마다 worktree · pre-push 훅) · G9 를 반영했다.
> rev 3 → rev 4: §3.5 교신 `exchange/1` · R1b(어떤 지시에도 속하지 않는 변경은 통합하지 않음) · 프롬프트의 교신 원칙을 반영했다(BD-133).

## 0. 제약과 정신

- Python ≥ 3.10, 핵심은 표준 라이브러리만. (git · pip 는 하위 프로세스로 부른다 — import 하지 않는다.)
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

