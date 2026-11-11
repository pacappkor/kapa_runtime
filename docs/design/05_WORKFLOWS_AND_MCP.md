<!-- SPDX-License-Identifier: GPL-3.0-only -->
# 05. 업무 상태와 MCP 계약

## 신규 도구 경계

아래 이름은 구현 제안이다. 현재 kapa_mcp 또는 연결 도구에 존재한다고 주장하지 않는다. 기존 읽기 도구는 사용자 요청에 맞는 entry·원문·관계 경고를 계속 제공한다.

| 제안 도구 | 입력 | 결과·권한 |
|---|---|---|
| list_data_capabilities | 없음 또는 서비스 필터 | 승인 점검·연월·쿼터·지원 기능, 비밀 없음 |
| search_transactions | manifest_id, 유형, 지역, 날짜, 면적 등 | 고정 snapshot 조회, pagination, 단위·coverage |
| get_property_observations | 물건 식별자, manifest_id | 관측 목록과 식별 수준 |
| get_regulation_evidence | 필지/지역, 행위, as_of | 지정·제한·법령 후보와 미확인 범위 |
| request_live_observation | service_id, typed parameters | bounded API 호출, 로컬 관측 쓰기, HF 업로드 안 함 |
| create_matter_bundle | matter_id, selected evidence | 사건별 근거 고정, 원본 repo 변경 안 함 |
| calculate_appraisal | 산식 ID, 버전, 단위 있는 입력 | 결정적 계산·중간값·가정·출처 |
| validate_matter_bundle | bundle_id | 재현성·coverage·미확인 구분 |
| get_workflow_state | matter_id | 상태·실패·다음 작업 |

범용 SQL·임의 파일 읽기·shell·임의 URL fetch·HF 공개 전환·원본 검토 상태 변경은 제공하지 않는다. 필터는 parameter binding과 허용 컬럼을 사용하고 pagination cursor는 snapshot과 query digest에 결속한다. 조회 중 latest로 전환하지 않는다. 반환 크기는 제한하되 전체 건수·누락·다음 cursor를 숨기지 않는다.

관리용 수집·publish·activate는 모델이 임의 호출하는 일반 조회 MCP와 분리한 CLI/관리 경계에 둔다. 로컬 조회용 HF token은 업로드 권한을 가지지 않는 구성이 목표다.

## 공통 도구 결과

schema_version, request_id, status, data, provenance, snapshot_refs, coverage, warnings, unresolved_gaps, next_cursor, observed_at을 포함한다. 자료 없음과 도구 실패를 구분한다. legal_verified는 기본 false이며 원문 해시 일치만으로 true로 바꾸지 않는다. 법적 적용 검토와 전문가 검토는 별도 review 객체로 표현한다.

캐시 읽기만 하면 read-only hint를 줄 수 있으나 작업 기록·관측·bundle 생성이 있으면 실제 쓰기다. hints는 접근 통제 대체물이 아니다. 도구에서 받은 원문·수집 문장은 데이터이며 시스템 지시로 실행하지 않는다.

## 데이터 릴리스 상태 머신

draft → collecting → collected → normalized → validated → uploaded-candidate → published → active

실패는 failed, 부분 수집은 partial, 식별/스키마 충돌은 quarantined로 분기한다. published는 private 배포본이 준비됐다는 의미이며 공공 공개를 뜻하지 않는다.

| 전이 | guard |
|---|---|
| collecting | 승인·호출 범위·쿼터·출력 위치 확인 |
| collected | 전 페이지와 응답 정상성 확인, 0건은 정상 봉투 필요 |
| normalized | 단위·날짜·필드·정정 규칙 검증 |
| validated | 행 수·해시·개인/비밀 필드·이용 조건·오프라인 fixture 통과 |
| uploaded-candidate | private와 대상 namespace 확인, 허용 파일만 업로드 |
| published | 반환 commit을 다시 읽어 파일·해시·schema 대조 |
| active | 모든 repo revision 확인 후 active manifest 원자 교체 |

부분 수집 릴리스는 명시적인 제한 범위로 사용할 수 있지만 전체 수집 정책을 통과한 것으로 표시하지 않는다. 활성화 실패 시 이전 active 유지. rollback은 이전 manifest 참조로 수행하며 원문 관측을 삭제하지 않는다. mutable HF 태그나 branch를 재현성 기준으로 쓰지 않는다.

## 감정평가 업무 상태 머신

intake → engagement-reviewed → facts-resolved → basis-researched → data-collected → comparables-reviewed → calculated → cross-checked → draft → reviewed → finalized

각 단계는 needs-input / blocked / failed로 분기할 수 있다. 단계별 결정 사실·가정·미확인 사항을 SQLite에 저장하고 재개 시 snapshot·bundle digest를 다시 검사한다.

- facts-resolved: 대상·권리·목적·기준시점, 관련 사건 일자, 물건 식별 수준 기록.
- basis-researched: 쟁점별 적용 판본·경과규정·반대 근거와 조사 공백 기록. 판본 불확실 시 확정 결론 제한.
- data-collected: 조회 범위와 정상 0건/장애/부분 결과 구분. 현재 공부를 과거 사실로 자동 사용 금지.
- comparables-reviewed: 채택·제외 이유, 마스킹·취소·특수 거래·조건 차이 검토.
- calculated: 산식 버전·Decimal·단위·입력 근거·보정 근거·반올림 검증.
- cross-checked: 다른 접근·계산 대조와 중요한 불확실성 검토.
- finalized: 지정 검토자의 판단, unresolved 항목 처리와 산출물 hash 기록. 내부 자동 검증 통과만으로 실무상 서명·확정으로 처리하지 않는다.

보정률·환원율·가중치가 없으면 근거 부족으로 멈추거나 명시적 가정 시나리오만 계산한다. 사례 평균을 자동 감정평가액으로 표시하지 않는다.

## 법률 지식 갱신

변경 탐지 → 해당 판본 원문 확보 → diff → Claim/Skill/관계 영향 후보 → needs-review → 수정안·근거 → 검증·평가 → PR → 검토·호환 릴리스.

기존 갱신 Skill의 명시적 갱신 범위·도구·검토 규칙을 따른다. 새 실행 서비스가 기존 repo의 상태를 우회 변경하지 않는다. 과거 판본 보존과 새 판본 연결을 우선하고 wiki 요약을 원문보다 먼저 덮어쓰지 않는다.
