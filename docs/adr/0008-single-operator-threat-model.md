<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0008: 단일 신뢰 운영자 로컬 PC 위협 모델

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `7e7b1f1`

## 배경

P0는 로컬 SQLite와 파일 snapshot을 사용하는 단일 운영 환경이다. 원격·로컬 동시 작성자를 지원하는 분산 시스템으로 해석하면 검증 경계를 과대평가하게 된다.

## 결정

운영자·실행 코드·로컬 장부 관리 권한을 신뢰하는 단일 PC를 전제로 한다. 외부 응답·원격 payload·수정 가능한 metadata는 검사 대상으로 삼아 비밀 검사, 경로/link/reparse 방어, 파일 크기·Parquet 확장 상한, 해시·고정 SHA 검증을 적용한다.

로컬 writer는 exclusive lock으로 중복 진입을 거부하고 stale lock을 자동 탈취하지 않는다. Hub 동시 변경은 parent/head 검사로 거부한다. 동시 로컬/Hub writer를 정상적으로 조정·병합하는 지원, 악성 로컬 관리자나 동일 프로세스 코드에 대한 격리, 모든 TOCTOU 경쟁의 제거는 보장 범위 밖이다. 마지막 head 검사 이후의 외부 변경까지 원격 잠금으로 막는 구조가 아니다.

## 대안

다중 사용자 서비스·분산 잠금·격리된 credential worker는 확장 대안이지만 현재 P0 계약보다 운영 복잡도가 크다.

## 결과

운영자는 다른 writer를 중단하고 lock/journal/head 문제를 확인한 후 복구한다. 방어 검사가 있다는 사실을 동시성 지원이나 로컬 침해 저항으로 표현하지 않는다.

## 재검토 조건

다중 운영자·스케줄러 중첩·비신뢰 플러그인·공유 PC 또는 지속적인 외부 Hub writer가 필요해지면 이 전제와 저장소/인증/조정 계층을 먼저 재설계한다.

## 근거

[단일 writer 장부](../../src/kapa_runtime/storage/state.py), [원격 검증](../../src/kapa_runtime/releases/hf.py), [파이프라인](../../src/kapa_runtime/pipeline.py), [운영 절차](../RUNBOOK.md).
