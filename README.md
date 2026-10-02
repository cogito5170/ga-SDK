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

## 시험

```sh
python -m unittest discover -s tests
```
