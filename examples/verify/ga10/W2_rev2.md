```ga
{"done_when": "이미 한 커밋을 report/1 의 commits 로 주장한 보고가 올라온다", "goal": "rlo-SDK 에 나란히 부르기 세기 도구를 더한다: Claude Code transcript(JSONL)에서 한 응답 안의 tool_use 수를 센다", "id": "CMD-WB1", "rev": 2, "schema": "directive/1", "scope": "rlo/parallel_calls.py 와 tests/test_parallel_calls.py (필요하면 tests/data/parallel*) 만. README 와 다른 파일은 이번에 고치지 않는다 — 이번 판은 새 코드 없이 보고만 다시 한다", "supersedes": {"id": "CMD-WB1", "rev": 1}, "to": "W2", "why": "K7 제안 3: 나란히 부른 호출은 훅 판정에서 deny D(문맥 불완전)가 된다. 실제로 얼마나 자주 나란히 부르는지 세어야 그 비용을 안다"}
```
## 고친 까닭
허브가 rev 1 보고를 받았지만 보고에 `commits` 가 없어 커밋을 통합할 수 없었다(R1b: 보고가 주장한 커밋만 통합한다). 또 `handled` 가 비어 있었다.

## 할 일
1. 새 코드는 쓰지 않는다. 이미 한 커밋이 맞는지 `git -C rlo log --oneline -3` 으로 본다.
2. `git -C rlo rev-parse HEAD` 로 40 자 sha 를 얻는다.
3. 아래 머리로 보고를 쓴다(<SHA> 만 바꾼다). 본문에 `## Result`(무엇을 했나, 시험 끝 줄)를 둔다.
```ga
{"schema": "report/1", "from": "W2", "handled": [{"id": "CMD-WB1", "rev_seen": 2, "status": "done"}], "commits": [{"repo": "rlo", "branch": "w2-parallel-calls", "sha": "<SHA>"}]}
```
