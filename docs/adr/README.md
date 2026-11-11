<!-- SPDX-License-Identifier: GPL-3.0-only -->
# 아키텍처 결정 기록

[원본 설계 v0.1](../design/README.md)은 역사적 사본이다. 아래 ADR은 `7e7b1f1`(P0 PR #1)의 코드와 [운영 절차](../RUNBOOK.md)를 기준으로 원본에서 유지·보완·변경한 결정을 기록하며, ADR-0001에는 PR #10의 data MCP 구현을 반영한다. 원본의 private-only, 미지 extra 보존, 공개 HFPublisher 계약을 현재 동작으로 해석하지 않는다. 실행은 운영 절차와 코드, 에이전트 작업은 [AGENTS](../../AGENTS.md)를 따른다. AGENTS의 P0 모듈 계약 중 역사적 설명보다 아래 변경 기록을 우선 확인한다.

| ADR | 결정 |
|---|---|
| 0001 | [저장소 책임 분리와 무자격증명 MCP 유지](0001-repository-boundaries.md) |
| 0002 | [HF market만 명시적으로 공개 허용](0002-hf-visibility.md) |
| 0003 | [Private 검증 후 공개 승격과 복구 journal](0003-staged-publication.md) |
| 0004 | [기본 거부 내부 HF writer와 파이프라인 intent](0004-internal-hf-writer.md) |
| 0005 | [운영 정책 파일과 보수적 호출 예산](0005-operator-policy.md) |
| 0006 | [데이터 스키마 v2와 기본 거부 extra 필드](0006-versioned-data-schema.md) |
| 0007 | [장부 결속 릴리스 신뢰와 경로 소유권](0007-publication-trust.md) |
| 0008 | [단일 신뢰 운영자 로컬 PC 위협 모델](0008-single-operator-threat-model.md) |

근거는 저장소 코드·RUNBOOK·AGENTS·Git 이력과 2026-11-10 세션/구조 검토 및 PR #1 검토 기록에 한정한다. 외부 오케스트레이터 기록은 출처 이름만 남기고 필요한 결론을 각 ADR에 담았으므로 읽기 위해 별도 workspace가 필요하지 않다. 대안과 재검토 조건은 현재 결정을 설명하는 기록이며 미래 기능의 구현 약속이 아니다.
