# baseline 실제 설정 (예시 · G8 시험 자료)

- `config.json`: cogito5170/baseline `PROTOCOL.md` §1 · §5 (2026-10-02) 의 세션 · 통로 · 소유표를 옮긴 것.
  Action · Guard · Health · SDK 세션은 브랜치 이름이 baseline 문서에 없어 넣지 않았다.
- `GUIDANCE.md` · `SESSION_GUIDANCE.md`: baseline `b22c2d1` 의 사본. 원본과 소유는 baseline 이다.

```
python -m ga prompt --config examples/baseline/config.json ga-SDK
python -m ga prompt --config examples/baseline/config.json --hub
```
