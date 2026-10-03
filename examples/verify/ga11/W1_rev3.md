```ga
{"done_when": "깨끗한 설치(pip install)본에서 rlo.suggest_model 이 import 되고 python -m rlo.suggest_model 이 돈다; 소스에 U+FFFD 가 없다; rlo-test 가 OK (skipped=2)", "goal": "rlo-SDK 에 suggest-model 명령을 더한다: hooks 의 --record 기록에서 모형(action-model/1)에 없는 도구 · 칸을 찾아 action-spec 초안을 낸다", "id": "CMD-WA1", "rev": 3, "schema": "directive/1", "scope": "rlo/suggest_model* · tests/test_suggest_model* · tests/data/suggest* 와 pyproject.toml(패키지 목록만). 그 밖의 파일, 특히 tests/test_versions.py 는 고치지 않는다", "supersedes": {"id": "CMD-WA1", "rev": 2}, "to": "W1", "why": "README 의 '넓히는 법'은 사람이 기록을 읽고 모형을 넓히게 한다(K7 에서 16 호출 중 4 가 모형 밖). 그 손일을 덜되 판단(위험 등급)은 사람에게 남긴다"}
```
## 고친 까닭 (baseline 의 거절, 운영자가 옮김)
- baseline 이 `claude/ga-trial-k9`@`6c33b85` 를 빈 venv 에 깨끗하게 설치하자 `import rlo.suggest_model` 이 `ModuleNotFoundError` 였다.
  `pyproject.toml` 의 `[tool.setuptools] packages = ["rlo"]` 는 하위 패키지 `rlo/suggest_model/` 을 넣지 않는다. PYTHONPATH 시험은 checkout 을 읽어서 초록이었다.
- `rlo/suggest_model/__init__.py:137` 의 note 문자열에 깨진 글자(U+FFFD) 하나가 있다("기록에서 찾은 새 칸" 이었을 것이다).

## 할 일
1. 먼저 통합 브랜치를 합친다(W2 의 일이 들어가 있다): `git -C rlo fetch origin claude/ga-trial-k9` 뒤 `git -C rlo merge --no-edit FETCH_HEAD`.
2. 설치본에 `rlo.suggest_model` 이 들어가게 고친다. 길은 네가 고른다 — 예: `packages` 에 `"rlo.suggest_model"` 을 더하거나, 하위 패키지를 모듈 파일 `rlo/suggest_model.py` 하나로 바꾼다(그러면 `python -m rlo.suggest_model` 도 그대로 돈다).
3. U+FFFD 를 바른 글자로 고친다.
4. `/tmp/claude-0/-home-user/8972da1e-25e6-5b19-9db6-e0a1d29ae15e/scratchpad/k9hub/bin/rlo-test ./rlo` 의 끝 줄이 `OK (skipped=2)` 인지 본다. 깨끗한 설치 검사는 네 자리에서 못 한다 — 허브가 통합 뒤 빈 venv 에 설치하고, 소스의 모든 모듈을 import 해 본다.
5. 커밋하고, 합친 머리를 `commits` 로 주장해 보고한다(머리 틀은 아래 '일하는 방법').

## 알아 둘 것
- `tests/test_versions.py` 의 `test_every_pin_agrees_with_this_list` 는 **깨끗한 설치 환경에서 원래(출발점 c6b2f95 부터) 실패한다**(설치 메타데이터가 `name@ url` 로 적혀 공백이 다르다). 네 일이 아니고 네 파일도 아니다. 고치지 않는다.
