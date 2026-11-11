<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0001: 저장소 책임 분리와 무자격증명 MCP 유지

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `7e7b1f1` 및 PR #10(data MCP)

## 배경

원본은 기존 저장 자료 조회와 신규 수집·릴리스를 분리했다. 2026-11-10 구조 검토도 저장소 수보다 중복 보안 구현과 호환 revision 관리가 실제 비용이라고 판단했다. 이 결정은 원본의 책임 경계를 유지한다.

## 결정

기존 저장소를 병합·폐기하지 않고 runtime을 별도 private 저장소로 둔다. 책임은 다음과 같다.

| 소유자 | 책임과 경계 |
|---|---|
| kapa_runtime | 외부 API 자격증명, 수집·정규화, 데이터 릴리스, 로컬 상태 |
| kapa_mcp | 기존 저장 자료 조회·근거 묶음·조판; 외부 키·HF 토큰·수집 코드 import 금지 |
| kapa_knowledge / kapa_skills | 지식·법령 및 skills 원본과 의존 pin; runtime에는 읽기 전용 입력 |
| kapa_edu_1 / kapa_edu_2 | 기존 교육 자료·빌드 계약; runtime에서 수정하지 않음 |
| docs/design / docs/adr | 원본 v0.1 보존 / 실제 구현의 보완·변경 기록 |

원본 설계 사본을 저장소에 포함해 단독 clone에서도 읽을 수 있게 한다. 기존 근거 묶음은 참조·digest로 합성하며 복제하지 않는다. data MCP는 기존 MCP에 권한을 추가하지 않고 별도 stdio 서버 `kapa-runtime-mcp`로 구현했다(선택 의존성 `mcp`). `list_data_capabilities`는 로컬 서비스 registry를 조회하고, `search_transactions`는 ACTIVE manifest와 로컬 장부에 결속되어 검증된 snapshot만 조회한다. 두 도구는 읽기 전용이며 자격증명·HF SDK를 불러오지 않고 네트워크 조회·수집·publish·activate를 수행하지 않는다.

## 대안

MCP 단일 서버와 credentialed worker, 공통 패키지 추출, edu 또는 skills/knowledge 통합을 검토했으나 권한·wire/domain 계약·고정 빌드 해시 차이 때문에 지금 적용하지 않는다.

## 결과

보안 책임과 원본 소유권은 명확하지만 저장소 간 pin 호환 확인과 중복 구현 검토가 필요하다. lock 불일치를 latest로 숨기지 않는다.

## 재검토 조건

P0 이후 실제 중복 비용이 측정되거나 단일 서버가 필요한 사용 사례가 확인되면 순수 함수 공통화와 권한 분리를 다시 검토한다.

## 근거

[AGENTS](../../AGENTS.md), [data MCP 서버](../../src/kapa_runtime/mcp/server.py), [ACTIVE snapshot 검증](../../src/kapa_runtime/storage/active.py), [선택 의존성과 실행 진입점](../../pyproject.toml). 외부 결정 기록: `_orchestrator/findings/repo-structure-review.md` (2026-11-10); 해당 책임 결론을 위 표에 옮겼으며 외부 파일은 clone의 필수 의존성이 아니다.
