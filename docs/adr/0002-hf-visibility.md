<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0002: HF market만 명시적으로 공개 허용

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `7e7b1f1`

## 배경

원본은 HF private 배포를 기본으로 했다. 사용자 결정(2026-11-10)은 검토한 공공데이터 유래 market에만 public을 허용했다.

## 결정

기본은 private이다. market만 명시적 `visibility: "public"`과 서비스별 이용조건 근거를 갖추면 공개할 수 있다. knowledge-index, evals, property, matters와 GitHub 저장소는 private, raw·로그는 로컬/비공개로 유지한다. 설정과 실제 가시성이 다르면 양방향 모두 거부하며 자동 변경·public fallback은 하지 않는다.

### 공개 manifest의 provenance 예외

사용자 결정(2026-11-11)에 따라 공개 market manifest는 재현성을 위해 private GitHub 저장소의 이름(`owner/repo`)과 전체 commit SHA를 의도적으로 포함한다. 이는 [workspace revision 고정](0009-workspace-revision-guard.md)의 provenance 공개이며, GitHub 저장소 자체나 private 원문·자격증명의 공개를 허용하는 결정이 아니다. 운영자 SID와 신뢰 소유자 설정도 이 공개 정보에 포함하지 않는다. 후속 구현 근거는 `1f3f877`의 [파이프라인](../../src/kapa_runtime/pipeline.py)과 [운영 절차](../RUNBOOK.md)이다.

## 대안

전부 private 고정은 사용자 공개 요구를 충족하지 못한다. 모든 역할의 공개 허용 또는 자동 visibility 변경은 비공개 자료의 경계를 약화한다.

## 결과

공개 여부는 업로드 성공을 위한 자동 복구 수단이 아니다. 운영자는 대상 identity·권한·이용조건을 먼저 확인해야 한다. 원본의 private-only 문구 대신 이 제한적 예외를 적용한다.

## 재검토 조건

사용자가 공개 범위를 변경하거나 제공기관 이용조건이 바뀌면 새 검토와 명시적 정책 변경이 필요하다.

## 근거

[설정](../../src/kapa_runtime/config.py), [HF writer](../../src/kapa_runtime/releases/hf.py), [운영 절차](../RUNBOOK.md). 사용자 결정 출처: `_orchestrator/SESSION-2026-11-10-runtime.md`의 USER POLICY 2026-11-10.
