# DESIGN — ga-SDK 0.1 설계 초안 (CMD-GA1 rev 1, 2026-10-02)

> 명세는 [`METHOD.md`](METHOD.md)(method-1, `78019c2`, baseline 소유)다. 이 문서는 **어떻게 짓는가**만 적는다.
> 상태: **초안 — 아직 코드 없음.** 아래는 계획이지 결과가 아니다.

## 0. 제약 (CMD-GA1 에서)

- Python ≥ 3.10, 핵심은 표준 라이브러리만. (git · pip 는 하위 프로세스로 부른다 — import 하지 않는다.)
- 저장소 · 세션 id · 브랜치 이름은 코드에 없고 모두 설정에서 온다.
- 1판: LLM 호출 없음, GitHub · 원격 세션 어댑터 없음(인터페이스만 그것을 담을 수 있게).

## 1. 패키지 모양

import 이름 `ga`, 배포 이름 `ga-sdk`.

```
ga/
  forms/        §3  directive/1 · report/1 · verdict/1 · round/1 · decision/1 · stage/1 · question/1
                    각 꼴 = dataclass + validate(dict) -> list[Problem] + parse/dump
  records.py    §3.4 기록 저장소(파일) + Markdown 렌더 (G2)
  rules.py      §5  R1–R13 검사기 -> list[Violation]  (G4)
  gates.py      §6  게이트 일곱 -> question/1 을 내고 멈춤 (G7)
  hub.py        §2  고리 1–6 (받기 → 통합 → 재현 → 판정 → 기록 → 다음) + tick (G3 · G5)
  config.py         허브 설정(JSON): 저장소 · 브랜치 · 소유표 · 시험 명령 · 환경 변수 · 예산
  adapters/
    base.py     §4  Channel · Waker · Vcs · Bundle · Ticker · Judge  (typing.Protocol)
    mailbox.py      파일 우편함 Channel + 없는 Waker
    git.py          Vcs (subprocess git)
    venv.py         Bundle (a) 경로 방식 · (b) 깨끗한 venv 에 sha 고정 설치
    human.py        Judge 사람 입력 구현(답을 파일에서 읽는다)
    ticker.py       Ticker: tick() 한 번 = 고리 [1] 한 번. 주기는 바깥(cron 등)이 정한다
  __main__.py       python -m ga  check | tick [--dry-run] | render | bundle
tests/              unittest (표준 라이브러리)
```

## 2. 형식의 겉모양 (§3 "사람이 읽는 텍스트 + 기계가 읽는 머리")

- 표준 라이브러리에 YAML 이 없으므로 **머리는 JSON** 으로 한다. 글 맨 앞의 ```` ```ga ```` 펜스 블록 하나에 JSON, 그 뒤가 사람 글.
  GitHub 댓글에서도 그대로 보이고, 파일 우편함에서도 같은 꼴이다.
- 모든 꼴에 `"schema": "directive/1"` 처럼 판이 붙는다. 모르는 판 · 모르는 칸 · 빠진 필수 칸은 거부한다.
- 지시 id 는 `CMD-<머리글자 1자 이상><번호>` (`CMD-GA1`, `CMD-K8` 모두 받는다).
- 기록은 **정규 JSON**(키 정렬 · `ensure_ascii=False` · 끝 줄바꿈)으로 쓰고 Markdown 은 거기서 렌더한다 → G2 의 "같은 입력이면 같은 바이트".

## 3. 고리와 상태

- 허브 상태 파일(`state.json`): 통로마다 마지막으로 읽은 글 id, 저장소마다 마지막 통합 sha.
  tick 은 이것과 비교해 새 것이 없으면 **아무것도 쓰지 않는다**(상태 파일도 안 건드림) → G5 · R9.
- 통합은 ff-only. 실패하면 합치지 않고 판정 `막힘(cause=의존성|구현)` 으로 넘긴다(R4).
- 통합 전 diff 를 훑어 R2(소유표 — 경로 glob → 세션) · R6(비밀값 패턴) 를 본다. 걸리면 통합하지 않는다.
- 판정 `verdict/1`: 숫자(시험 수 · 건너뜀 · 보고 수와 재현 수의 차)는 기계가 채우고, `class` · `next` 는 `Judge` 가 제안한다.
  1판의 `Judge` 는 사람 입력이므로 시험에서는 미리 적어 둔 답 파일을 쓴다.
- 엇갈림(F1): 허브는 지시의 최신 `rev` 를 알고, 보고의 `handled[].rev_seen` 이 그보다 낮으면 `부분 성공(엇갈림)` 으로 가른다.

## 4. Bundle (G3 · F3 · F4)

- (a) 경로 방식: 저장소들을 통합 머리에 체크아웃하고 `PYTHONPATH` 에 나란히 둔 채 설정의 시험 명령을 돈다.
- (b) 설치 방식: 빈 venv 에 `pip install <repo>@<sha>` 를 묶음 전체로 한 번에 넣는다. `ResolutionImpossible` 류 실패를 `고정 충돌` 로 가른다.
- 시험 결과는 unittest/pytest 요약 줄을 읽어 통과 · 실패 · 건너뜀 수로 만든다. **건너뜀 수는 판정에 올린다**(F4).

## 5. 검증 계획 (§9)

| # | 무엇 | 필요한 것 | 지금 |
|---|---|---|---|
| 1 | stage-2 · 3 · 4 고정 sha 에서 저장소별 시험 수 재현 | Telemetry · Sensor · MS · DC · action · guard · health · rlo-SDK 읽기 권한 | 아직 이 세션에 없음 — 짓는 동안 붙여 본다. 안 되면 `막힘` 으로 보고 |
| 2 | A4/A5 · K1 엇갈림을 파일 우편함으로 흉내 | 없음 | 시험으로 짓는다 |
| 3 | action `2f4791e` + `3995fdb` 섞은 고정에서 (b) 실패 | action 저장소 읽기 + pip 의 GitHub 접근 | 1 과 같음 |

## 6. 위험 · 가정

- (가정) 시험 환경에서 (b) 의 로컬 git 저장소 설치는 PyPI 접근 없이 돌아야 한다 → `--no-build-isolation` + 시스템 setuptools 로 짓는다. 실제 stage 재현은 네트워크가 필요하다.
- (가정) 시험 수는 각 저장소의 시험 명령 출력에서 읽는다. 명령은 설정으로 받고, 지금 baseline 이 쓴 명령을 기본값 예시로 둔다.
