# ga-SDK

허브 세션 하나가 지시와 보고로 작업 세션 여럿을 굴리는 고리를 기계로 돌린다. 명세는 [`METHOD.md`](METHOD.md)(baseline 소유)에 있고, 짓는 방법은 [`DESIGN.md`](DESIGN.md)에 있다.

1판(0.1)은 로컬에서만 돈다. 쓰는 것은 파일 우편함, git worktree, venv, 수동 Runner뿐이다. 원격 세션, GitHub, LLM, 네트워크 없이 돌아간다. Python 3.10 이상이 필요하고, 핵심은 표준 라이브러리만으로 짓는다.

## 설치

```sh
pip install -e .
```

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

더 강하게 지키려면 원격이 사람을 직접 확인해야 한다(예: GitHub 브랜치 보호). 세션마다 OS 사용자 · 컨테이너를 따로 두는 길도 있다.

## 시험

```sh
python -m unittest discover -s tests
```
