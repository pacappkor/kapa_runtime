<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0003: Private 검증 후 공개 승격과 복구 journal

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `7e7b1f1`

## 배경

원본의 후보 업로드·검증만으로는 public에 잘못 게시된 바이트를 사후 검증 전에 숨길 수 없다. 강제 종료와 원격 head 변경도 복구에 영향을 준다.

## 결정

public market은 별도 private staging에 먼저 올려 고정 SHA의 전체 inventory를 검증하고 같은 바이트만 단일 public commit으로 승격한다. 후보 외에도 검증된 retained partition과 baseline에 결속된 `.gitattributes`를 포함한다. private-only 게시에는 이 추가 단계를 요구하지 않는다.

승격 전에 SQLite journal에 복원 inventory·intent를 저장한다. 실패·중단 시 journal의 예상 head에 결속된 복원 commit을 검증한 뒤 quarantine하며, 복원 실패 시 journal을 보존하고 다른 게시를 막는다. 예상 head가 없거나 달라지면 임의 채택·덮어쓰기 없이 거부한다. inventory commit의 `parent_commit`, 검증 후 및 published 전 head/visibility 비교로 동시 변경을 감지·거부한다.

## 대안

public 직접 업로드 후 검증, active snapshot만을 복원 기준으로 사용, 최신 head를 자동 채택하는 방법은 각각 사전 검증·전체 보존 inventory·동시 작성자 보호를 잃는다.

## 결과

staging 저장공간과 네트워크 비용이 늘어난다. 복원은 default branch 복구이지 이미 생성된 public commit 이력 삭제가 아니다. 첫 공개 게시에 신뢰된 복원 근거가 없으면 비어 있거나 검증된 attributes만 있는 repo만 허용한다. 동시 작성자를 병합·조정하는 기능은 없다.

## 재검토 조건

다중 writer, 원격 트랜잭션 또는 공개 이력 삭제 보장이 요구되면 배포 저장소와 조정 프로토콜을 재설계한다.

## 근거

[pipeline publish/복구](../../src/kapa_runtime/pipeline.py), [HF inventory 교체](../../src/kapa_runtime/releases/hf.py), [journal](../../src/kapa_runtime/storage/state.py), [운영 절차](../RUNBOOK.md).
