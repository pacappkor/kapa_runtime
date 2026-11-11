<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0004: 기본 거부 내부 HF writer와 파이프라인 intent

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `7e7b1f1`

## 배경

초기 모듈 계약의 공개 `HFPublisher`와 범용 파일 교체 경로는 호출자가 게시 검증을 우회할 여지를 만들었다.

## 결정

HF 쓰기는 공개 API가 아닌 내부 `_HubWriter`에 두고 게시·복원은 pipeline 명령을 통해 수행한다. public 직접 candidate 업로드는 거부하며 private 검증 결과·대상 repo·전체 payload hash가 결합된 승격 intent만 허용한다. 복원은 state store가 journal digest·release ID·repo ID·예상 head를 확인해 발급한 authority로 저장된 정확한 inventory만 허용한다.

비어 있지 않은 secret scan 집합, 명시적 대상/가시성, 허용 경로·스키마와 실제 바이트 검사를 요구한다. 주입 API token도 검사 집합에 추가한다. 현재 terms 승인이 철회되어도 제한된 복원은 가능하지만 신규 게시 승인은 우회할 수 없다.

## 대안

공개 범용 writer에 선택적 검사 플래그를 두거나 호출자가 복원 payload dict를 전달하는 방법은 기본 거부 경계를 유지하지 못한다.

## 결과

저수준 재사용 편의보다 검증 순서를 우선한다. 내부 이름과 seal은 신뢰된 프로세스의 오용 방지 장치이지 악성 로컬 Python 코드에 대한 격리 경계는 아니다.

## 재검토 조건

외부 플러그인이나 비신뢰 호출자에게 쓰기를 제공해야 하면 별도 프로세스·권한·서비스 인증 경계를 설계한다.

## 근거

[내부 writer와 승격 intent](../../src/kapa_runtime/releases/hf.py), [복원 authority](../../src/kapa_runtime/storage/state.py), [파이프라인](../../src/kapa_runtime/pipeline.py), [운영 절차](../RUNBOOK.md).
