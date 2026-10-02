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

## 시험

```sh
python -m unittest discover -s tests
```
