# BASELINE SESSION INTERACTION GUIDANCE (사용자 제공, 2026-10-02 — 원문 보관)

> 이 문서는 새 규칙이나 별도 거버넌스가 아니다. baseline 이 세션에 지시하거나 결과를 평가할 때 놓치기 쉬운 것을 확인하는 참고다.
> 지금 workflow 가 잘 돌면 그대로 둔다. 매 interaction 에서 세 가지만 분명히 한다:
> **A. 지금 작업이 왜 필요한가 · B. 실제로 무엇이 달라졌는가 · C. 다음 interaction 이 왜 필요한가.**

1. 먼저 현재 상태를 이해한다 — 기준선 · 이전 interaction · 최근 결과 · 저장소 상태 · 이미 해결된 문제. 이미 된 것을 다시 시키지 않는다. 잘 작동하는 구조를 다시 설계하지 않는다.
2. 역할 경계를 참고한다(절대 규칙 아님) — Sensor: 무엇을 관측 · Telemetry: 무엇이 일어났나를 기록/전달 · DC: 이번 결정에 무엇이 필요한가 · Runtime(MS): 그것으로 policy/execution. 경계가 다른 편이 합리적이면 이유와 영향을 확인한다.
3. 제안과 결과를 구분한다 — "다음에 X 하겠다" 는 계획, "X 를 했고 Y 에서 Z 가 측정됐다" 는 결과.
4. 결과보다 evidence — 중요한 주장에는 test · benchmark · log · diff · 저장소 상태 · 측정값 · 재현 실험 중 하나. 작은 변경에 과한 검증을 요구하지 않는다.
5. acceptance 를 가능하면 명확히 — 무엇을 개선하나 · 무엇이 달라지면 성공 · 어떻게 확인. 기존 metric · 비교 기준을 우선.
6. 세션 간 중복 주의 — 누가 관측 · 전달 · 의미 부여 · 사용하나로 책임을 가른다.
7. 충돌은 바로 해결하지 않는다 — 왜 다른가 → 각 근거 → 기준선과 비교 → 필요하면 작은 실험.
8. 영향 범위 — 중요한 interface/schema 변경은 downstream 영향을 확인. 작은 변경까지 모두를 재검토하게 하지 않는다.
9. 기준선을 쉽게 흔들지 않는다 — local implementation / component-level / architecture-baseline-level 을 가른다.
10. 실패도 결과 — 성공 · 부분 성공 · 실패 · 막힘 · 정보 부족. 원인: 구현 · 요구사항 · 의존성 · 측정 · 환경.
11. 반복 작업 주의 — 이미 검증된 것 · 이미 실패한 것 · 진행 중인 것 · 다른 세션이 하는 것을 가른다.
12. 모든 문제를 지금 풀지 않는다 — nice-to-have · future improvement · architectural concern · blocking issue.
13. 너무 구체적인 구현을 강제하지 않는다 — 목표와 제약을 주고 방법은 세션에 맡긴다. 이미 명확한 interface 는 존중한다.
14. 끝났다고 바로 다음 작업을 만들지 않는다 — 무엇이 바뀌었나 · 검증됐나 · 새 문제 · 다른 세션 영향 · 목표가 여전히 유효한가 → continue · refine · verify · handoff · change direction · wait.
15. interaction 자체도 정보다 — 반복되는 문제 · 계속 막히는 세션 · 충돌하는 interface · 계속 부족한 정보 · 불필요한 반복은 기준선/architecture 신호일 수 있다.
16. 목적은 세션에게 많은 일을 시키는 것이 아니라 loop(기준선 → 지시 → 실행 → 결과+evidence → 판단 → 다음)가 건강한 것이다.
