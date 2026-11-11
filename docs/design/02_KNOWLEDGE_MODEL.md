<!-- SPDX-License-Identifier: GPL-3.0-only -->
# 02. 지식 모델과 에이전트 컨텍스트

## 기술별 역할

| 기술 | 적용 방식 | 최초 산출물 |
|---|---|---|
| LLM Wiki | 사람이 읽을 쟁점별 안내, 원문·판본·반대 근거 연결 | 기존 issues 확장안 |
| Ontology | 개체·관계·필드 의미를 명시, 자동 법률 추론과 구별 | concepts 및 관계 계약 |
| Knowledge Graph | 기존 JSONL 관계 활용, 인용과 적용 판단 분리 | 재생성 가능한 nodes/edges |
| Agent Skills | 절차·필수 입력·종료 조건·실패 대응 | 기존 workflow와 신규 조회 절차 연결 |
| Rules / Policies | 도구 경계·판본·근거·확정 조건 강제 | 정책 필드와 코드 validator |
| Structured Knowledge | 안정 ID, typed observation, 단위·날짜 | JSON Schema, Parquet schema |
| Agent Memory | 사용자 선호·사건 진행을 사건별 저장 | preferences와 checkpoint |
| Context Engineering | 필요한 원문·사실·도구 결과만 구성 | context packet |
| Tool / API Grounding | 공식 조회 및 결정적 계산 | 서비스 어댑터·계산 모듈 |
| Validation / Eval | 구조·데이터·근거·계산·업무 평가 | 회귀 fixture와 전문가 검토셋 |
| Decision Records | 기술 선택과 사건 판단의 사유 | ADR 및 matter decision |
| Workflow / State Machine | 상태별 입력·출력·통과 조건 | 상태 전이 계약 |

## ID와 권위 보존

기존 `law/<lsId>@<lsiSeq>#a<article>`, `apps@<admRulSeq>#<code>`, `case/<court>/<case_no>`, `interp/<agency>/<case_no>`, `issue/<slug>`, `skill/<name>` 계약을 재사용한다. 정확한 문법은 고정 커밋의 `kapa_knowledge/docs/SCHEMA.md`를 우선한다.

source / summary / analysis / machine 구분, progress와 reference_status의 분리, 검토 원문 해시 결속을 유지한다. FAQ·동일성 보류 자료·과거 판본 메타데이터를 정식 해석 또는 확인한 과거 조문 본문으로 승격하지 않는다. 기존 스키마가 허용하지 않는 개체는 먼저 제안 스키마로 만들고 마이그레이션·검토 후 편입한다.

## Claim 확장 제안

문서 요약과 별도로 중요한 주장을 관리한다. Claim은 법률 규칙 그 자체가 아니라 적용 조건과 근거를 갖춘 검토 가능한 주장이다.

| 필드 | 의미 |
|---|---|
| claim_id | 신규 안정 ID, 기존 ID와 충돌 금지 |
| statement | 한 가지 판단 내용 |
| issue_ids | 연결 쟁점 |
| applicability | 사실 조건·관할·시간·적용 법령판본 |
| exceptions | 예외와 적용 제한 |
| evidence | source ID·revision·hash·정확한 위치/인용 |
| counter_evidence | 반대·구별 근거 |
| review_status | draft / needs-review / accepted / rejected |
| reviewer | human 또는 agent 구분, 식별자·모델 버전·검토시각 |
| provenance | 원문·요약·분석·기계 산출 구분 |

accepted는 내부 검토 상태다. 모든 사건에 적용 가능하거나 인간 전문가 검토가 완료됐다는 의미로 사용하지 않는다.

## 관계

기존 관계 타입을 먼저 조사하고 중복되는 타입을 새로 만들지 않는다. 신규 후보는 cites, interprets, supports, contradicts, requires-fact, affected-by다. overrules/distinguishes의 기존 확정·증거 요건을 완화하지 않는다. 관계별로 src/dst 타입, 근거 위치, 생성 방법, 검토 상태를 정의한다.

인용 관계에서 적용 가능성을 자동 도출하지 않는다. 후일 판결이라는 이유로 종전 판례 변경을, 오래된 판결이라는 이유로 역사 자료 전용을 추정하지 않는다. 조문 개정은 영향 검토 후보를 만들며 기존 판례·Claim의 결론을 자동 수정하지 않는다.

## Context packet

각 업무 단계는 다음 순서의 packet을 구성한다.

1. 의뢰 목적·대상·권리·기준시점과 쟁점별 사건 일자.
2. 확정 사실 / 사용자 주장 / 가정 / 미확인 사실.
3. 적용 후보 판본과 미해결 경과규정.
4. 관련 원문 발췌, 문서 ID·revision·위치.
5. 반대 근거·예외·미검토 검색 범위.
6. 데이터 관측·조회 범위·단위·누락·시점.
7. 계산 입력·산식 버전·결과·검증.
8. 다음 작업과 결론 확정을 막는 조건.

전체 Wiki를 상주시키지 않는다. 정형 필터와 키워드 검색을 먼저 적용하고 필요하면 임베딩을 보조한다. 임베딩의 모델 ID·revision·차원·chunker·원문 해시·생성일을 고정한다. 검색 점수를 법적 권위나 참조 상태로 사용하지 않는다.

## Memory와 Decision Record

- preferences: 문서 형식·언어·단위 표기 등 명시적인 사용자 선호.
- matter memory: 선택한 snapshot, 미확인 사실, 완료 단계, 재개 checkpoint.
- 지식으로 재사용할 사건 판단: 비식별화·근거 정리·검토 후 Knowledge 제안으로 전환.
- matter decision: 선택한 방법·채택/제외 사례·보정치·대안·근거·판단자·시각.
- ADR: 기술 결정의 배경·선택·대안·영향·재검토 조건.

사건 간 고객 정보와 가정이 섞이지 않도록 matter_id를 모든 상태·검색 범위·출력에 전달한다. 미검토 대화 내용을 공통 법률 지식으로 자동 승격하지 않는다.
