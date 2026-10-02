# METHOD §9 검증 결과 (2026-10-02, ga-SDK 0.1)

돌린 명령은 `examples/verify/replay_stages.py`다. 저장소들은 각자의 통합 브랜치 `claude/gracious-meitner-vp49xe`를 clone한 것이다. 환경은 Python 3.11이고, git은 공개 저장소를 익명으로 읽었다.

## 검증 1 — 단계 고정 재현: 22 / 22 일치 (verified)

baseline `STAGES.md`의 stage-2, stage-3, stage-4 표를 그대로 읽었다. 저장소마다 기록된 sha에 둔 채 `Bundle` (a)로 돌렸다. 저장소를 나란히 놓고 PYTHONPATH로 잇는 방식이며, baseline이 시험한 방식과 같다.

| 단계 | 저장소 | 기록 | 재현(통과 / 실패 / 건너뜀) |
|---|---|---|---|
| stage-2 | Telemetry `d60d591` | 70 | 70 / 0 / 0 |
| stage-2 | Sensor `10bb7ad` | 225 | 225 / 0 / 0 |
| stage-2 | MS `2cb7e61` | 185 | 185 / 0 / 0 |
| stage-2 | DC `ce3a0bc` | 105 | 105 / 0 / 0 |
| stage-2 | action `443f8eb` | 25 | 25 / 0 / 0 |
| stage-2 | guard `6e4ad56` | 81 | 81 / 0 / 0 |
| stage-2 | health `a07d833` | 26 | 26 / 0 / 0 |
| stage-3 | Telemetry `89d2887` | 72 | 72 / 0 / 0 |
| stage-3 | Sensor `a073e77` | 246 | 246 / 0 / 0 |
| stage-3 | MS `74a8585` | 212 | 212 / 0 / 0 |
| stage-3 | DC `b55ff04` | 108 | 108 / 0 / 0 |
| stage-3 | action `2f4791e` | 68 | 68 / 0 / 0 |
| stage-3 | guard `be871b9` | 93 | 93 / 0 / 0 |
| stage-3 | health `afcff39` | 36 | 36 / 0 / 0 |
| stage-4 | rlo-SDK `d313414` | 62 | 60 / 0 / **2** |
| stage-4 | Telemetry `6b9dd42` | 75 | 75 / 0 / 0 |
| stage-4 | Sensor `97961e9` | 255 | 255 / 0 / 0 |
| stage-4 | MS `19d850e` | 215 | 215 / 0 / 0 |
| stage-4 | DC `526f2fb` | 116 | 116 / 0 / 0 |
| stage-4 | action `c27840a` | 68 | 68 / 0 / 0 |
| stage-4 | guard `be871b9` | 93 | 93 / 0 / 0 |
| stage-4 | health `afcff39` | 36 | 36 / 0 / 0 |

- rlo-SDK의 건너뜀 2는 STAGES.md에 적힌 "rlo-SDK 건너뜀 2 = 설치 메타데이터 시험"과 같다. ga도 건너뜀을 숨기지 않고 따로 셌다(F4).
- **처음 한 번은 DC가 어긋났다.** 기록은 105 / 108 / 116이었는데, 재현은 21–24 통과에 오류 3–4였다.
  - 원인: 내 시험 명령이 `-s tests`뿐이었다. DC의 `tests/`는 패키지라서 상대 import를 쓰고, 그래서 `-t .`이 있어야 돈다.
  - 고친 뒤: `tests/__init__.py`가 있으면 `-t .`을 붙이게 했다. 그러자 DC도 기록과 같아졌다.
  - ga는 이 어긋남을 숨기지 않았다. 실패로 드러냈다(ok=false, errors>0). 저장소마다 시험 명령을 설정으로 받는 이유가 이것이다.

## 검증 2 — 엇갈림 재현 (verified, 시험)

`tests/test_loop.py`의 `CrossingTest`가 파일 우편함으로 흉내 낸다.
- **A4(51 회차):** 덧붙임(rev 2)을 읽기 전에 끝냈다. `rev_seen [1, 1]`이다. 결과는 부분 성공(엇갈림)이고 원인은 허브 지시다. 지시는 열린 채로 남는다.
- **A5(53 회차):** A6이 대체한 뒤에 실행했다. 결과는 엇갈림이고, "A5가 A6으로 대체됨"이라는 근거가 붙는다.
- **K1:** 옮길 원본 sha가 rev 2에서 바뀌었다. `rev_seen 1`이다. 결과는 엇갈림이다.
- **대조:** 끝내기 전에 덧붙임을 읽은 경우(`[1, 2]`)는 성공이다. 이 세션의 rev 1 → rev 2 보고가 실제로 이 경우였다.

## 검증 3 — 고정 충돌 재현 (verified)

`Bundle` (b)로 빈 venv에 sha를 고정해 설치했다. pip이 GitHub에서 받았다.

| action | guard(`action@3995fdb` 고정) | 결과 |
|---|---|---|
| `2f4791e` | `be871b9` | **pin_conflict** (`ResolutionImpossible`) |
| `3995fdb` | `be871b9` | 설치 성공 |

설치에 성공한 쪽에서 시험을 돌린 결과는 다음과 같다(참고).
- action@3995fdb: 오류 3, 건너뜀 13
- guard: 88 통과, 건너뜀 5
- action의 오류 3은 시험이 `guard.params`와 `guard.predicate`를 부르기 때문에 났다. guard는 be871b9에서 이 두 모듈을 지웠다(BD-116 "흔적 두 파일 지움").
- 따라서 이 오류는 ga의 문제가 아니라 두 판 사이의 시험 짝 문제다.
- 두 저장소만 깔았기 때문에 옆 저장소를 쓰는 시험은 건너뛰었다. 이 시험 수는 stage 기록과 비교할 대상이 아니다.
