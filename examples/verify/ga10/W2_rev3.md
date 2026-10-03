```ga
{"done_when": "w2-parallel-calls 가 통합 브랜치 claude/ga-trial-k9 를 포함하고, 그 위에서 rlo-test 가 OK (skipped=2), 합친 머리를 commits 로 주장한 보고가 올라온다", "goal": "rlo-SDK 에 나란히 부르기 세기 도구를 더한다: Claude Code transcript(JSONL)에서 한 응답 안의 tool_use 수를 센다", "id": "CMD-WB1", "rev": 3, "schema": "directive/1", "scope": "rlo/parallel_calls* · tests/test_parallel_calls* 만 — 이번 판은 통합 브랜치를 합치고 다시 보고한다(새 기능 없음)", "supersedes": {"id": "CMD-WB1", "rev": 2}, "to": "W2", "why": "K7 제안 3: 나란히 부른 호출은 훅 판정에서 deny D(문맥 불완전)가 된다. 실제로 얼마나 자주 나란히 부르는지 세어야 그 비용을 안다"}
```
## 고친 까닭
허브가 W1 의 커밋을 통합 브랜치 `claude/ga-trial-k9` 에 먼저 넣었다. 네 브랜치는 그 앞에서 갈라져 ff 가 아니라서(R4) 통합하지 못했다.

## 할 일
1. `git -C rlo fetch origin claude/ga-trial-k9` 로 통합 브랜치를 가져온다.
2. `git -C rlo merge --no-edit FETCH_HEAD` 로 합친다. 네 파일과 겹치는 곳은 없을 것이다(W1 은 rlo/suggest_model* 만 바꿨다).
3. `/tmp/claude-0/-home-user/8972da1e-25e6-5b19-9db6-e0a1d29ae15e/scratchpad/k9hub/bin/rlo-test ./rlo` 의 끝 줄이 `OK (skipped=2)` 인지 본다.
4. `git -C rlo rev-parse HEAD` 로 합친 머리의 40 자 sha 를 얻고, 아래 머리로 보고한다(<SHA> 만 바꾼다). 본문에 `## Result` 와 시험 끝 줄.
```ga
{"schema": "report/1", "from": "W2", "handled": [{"id": "CMD-WB1", "rev_seen": 3, "status": "done"}], "commits": [{"repo": "rlo", "branch": "w2-parallel-calls", "sha": "<SHA>"}]}
```
