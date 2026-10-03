```ga
{"done_when": "이미 한 커밋을 report/1 의 commits 로 주장한 보고가 올라온다", "goal": "rlo-SDK 에 suggest-model 명령을 더한다: hooks 의 --record 기록에서 모형(action-model/1)에 없는 도구 · 칸을 찾아 action-spec 초안을 낸다", "id": "CMD-WA1", "rev": 2, "schema": "directive/1", "scope": "rlo/suggest_model.py 와 tests/test_suggest_model.py (필요하면 tests/data/suggest*) 만. README 와 다른 파일은 이번에 고치지 않는다 — 이번 판은 새 코드 없이 보고만 다시 한다", "supersedes": {"id": "CMD-WA1", "rev": 1}, "to": "W1", "why": "README 의 '넓히는 법'은 사람이 기록을 읽고 모형을 넓히게 한다(K7 에서 16 호출 중 4 가 모형 밖). 그 손일을 덜되 판단(위험 등급)은 사람에게 남긴다"}
```
## 고친 까닭
허브가 rev 1 보고를 받았지만 보고에 `commits` 가 없어 커밋을 통합할 수 없었다(R1b: 보고가 주장한 커밋만 통합한다).

## 할 일
1. 새 코드는 쓰지 않는다. 이미 한 커밋이 맞는지 `git -C rlo log --oneline -3` 으로 본다.
2. `git -C rlo rev-parse HEAD` 로 40 자 sha 를 얻는다.
3. 아래 머리로 보고를 쓴다(<SHA> 만 바꾼다). 본문에 `## Result`(무엇을 했나, 시험 끝 줄)를 둔다.
```ga
{"schema": "report/1", "from": "W1", "handled": [{"id": "CMD-WA1", "rev_seen": 2, "status": "done"}], "commits": [{"repo": "rlo", "branch": "w1-suggest-model", "sha": "<SHA>"}]}
```
