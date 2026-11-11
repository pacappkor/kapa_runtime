<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0005: 운영 정책 파일과 보수적 호출 예산

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `7e7b1f1`

## 배경

원본 예시의 승인·quota 미확정 값은 운영 승인 근거가 아니다. API 호출 가능성과 외부 저장·재배포 허가는 별도로 확인해야 한다.

## 결정

data_root의 `operator-policy.json`에 서비스별 `terms_reviewed`, 이용조건·출처·검토일을 담은 `terms_basis`, `max_requests`, 테스트한 깨끗한 전체 Git SHA의 fixture 근거를 기록한다. build와 publish는 이용조건 근거 digest를 결속하며 정책 변경 시 새 ID로 수집·build한다. `legal_verified=false`를 유지한다.

호출 예산은 서비스별 3~150회, 페이지당 재시도 포함 3회를 예약한다. data.go.kr API별 10,000회/일·VWorld 40,000회/일은 사용자 보고(2026-11-10)이지 제공기관 검증이 아니다. VWorld 수집은 미구현이다. P0는 Asia/Seoul 날짜·서비스별로 전체 run 예산을 SQLite에 선예약하며 자정 이후 새 날짜에도 요청 전에 예약한다. 실패·미사용분을 환급하지 않는다.

## 대안

`quota=null`을 승인으로 취급하거나 실제 성공 호출만 사후 차감하면 불확실한 한도와 재시도·자정 경계를 안전하게 다루지 못한다.

## 결과

예산을 보수적으로 소모한다. 다른 프로그램의 사용량은 알 수 없으므로 운영자가 잔액·승인·이용조건을 별도 확인한다. 테스트 통과나 해시 일치는 법적 검증을 뜻하지 않는다.

## 재검토 조건

제공기관의 검증된 한도·약관이 바뀌거나 여러 실행 주체가 예산을 공유하면 정책 근거와 예약 체계를 재검토한다.

## 근거

[운영 절차](../RUNBOOK.md), [collect/build/publish](../../src/kapa_runtime/pipeline.py), [예약 장부](../../src/kapa_runtime/storage/state.py). 사용자 보고 출처: `_orchestrator/SESSION-2026-11-10-runtime.md`.
