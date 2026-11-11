<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0007: 장부 결속 릴리스 신뢰와 경로 소유권

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `7e7b1f1`

## 배경

수정 가능한 로컬 manifest나 경로명만 믿으면 과거 릴리스를 이용해 변조된 바이트를 보존·복구할 수 있다. 삭제된 public 경로가 staging에서 다시 유입되는 문제도 있었다.

## 결정

build는 validated canonical digest·파일 근거를, published 전이는 `published_manifest_sha256`을 장부에 기록한다. 조회·활성화·rollback·보존·복구는 이 digest에 결속된 manifest만 신뢰한다. digest 없는 legacy 행은 자동 채택하지 않는다. 운영자의 `attest-legacy`는 장부에 이미 기록된 고정 원격 SHA와 업로드 candidate를 읽고 hash·rows·스키마·현재 비밀 검사 및 최종 manifest canonical equality를 확인한 뒤에만 `legacy_attestation` 근거를 저장한다. 로컬 불일치를 수정해 맞추지 않는다.

보존 경로 소유자는 현재 active 우선, 아니면 신뢰된 게시 시각의 최신 소유자로 결정한다. 그 소유자의 정확한 hash·rows·schema만 허용하며 검증된 사본으로 수리한다. 복구 불가능한 active 경로는 거부하고 비활성 경로 제거는 장부에 기록한다. repo별 tombstone은 staging/public 소유권 합집합 뒤에 적용하여 삭제 경로의 부활을 막는다. 과거 활성화는 대상 게시 시점 소유권과 현재 head에서 대상 partition의 동일 바이트 제공 여부를 모두 확인한다.

## 대안

로컬 파일에서 digest 자동 보충, release ID 사전순 소유자 선택, 임의 과거 hash 일치 허용, staging의 제거 누락을 public 재게시 허가로 해석하는 방법은 신뢰 근거를 약화한다.

## 결과

과거 릴리스가 존재해도 신뢰 근거 또는 현재 제공 partition이 없으면 활성화가 거부된다. 운영자 attestation도 원격 읽기 검증이며 신규 게시나 법적 증명이 아니다.

## 재검토 조건

서명된 외부 장부, 다중 운영자 승인 또는 새 보존 정책이 필요하면 digest 신뢰 루트와 소유권 시간 규칙을 재검토한다.

## 근거

[신뢰 manifest·소유권·attestation](../../src/kapa_runtime/pipeline.py), [장부 근거](../../src/kapa_runtime/storage/state.py), [운영 절차](../RUNBOOK.md). tombstone 검토 계기: `_orchestrator/findings/runtime-pr1-review-22.md`.
