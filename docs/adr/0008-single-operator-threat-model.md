<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0008: 단일 신뢰 운영자 로컬 PC 위협 모델

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `7e7b1f1`

## 배경

P0는 로컬 SQLite와 파일 snapshot을 사용하는 단일 운영 환경이다. 원격·로컬 동시 작성자를 지원하는 분산 시스템으로 해석하면 검증 경계를 과대평가하게 된다.

## 결정

운영자·실행 코드·로컬 장부 관리 권한을 신뢰하는 단일 PC를 전제로 한다. 외부 응답·원격 payload·수정 가능한 metadata는 검사 대상으로 삼아 비밀 검사, 경로/link/reparse 방어, 파일 크기·Parquet 확장 상한, 해시·고정 SHA 검증을 적용한다.

로컬 writer는 exclusive lock으로 중복 진입을 거부하고 stale lock을 자동 탈취하지 않는다. Hub 동시 변경은 parent/head 검사로 거부한다. 동시 로컬/Hub writer를 정상적으로 조정·병합하는 지원, 악성 로컬 관리자나 동일 프로세스 코드에 대한 격리, 모든 TOCTOU 경쟁의 제거는 보장 범위 밖이다. 마지막 head 검사 이후의 외부 변경까지 원격 잠금으로 막는 구조가 아니다.

### Workspace sibling guard의 소유권 경계

후속 구현 기준 `1f3f877`에서 workspace guard의 보안 경계는 Git 실행 전 **소유권 gate**이다. checkout, Git 디렉터리와 실제 Git 설정 파일의 소유자는 현재 실행 사용자, Git과 동등한 Windows `BUILTIN\Administrators` 규칙으로 허용되는 소유자, 또는 `trusted_repo_owners`에 명시적으로 승인한 identity여야 한다. 관리자 규칙은 실행 토큰에서 관리자 그룹 membership이 활성화된 경우에만 적용하며, 계정이 그룹에 등록되어 있다는 사실만으로 허용하지 않는다. 알 수 없거나 읽을 수 없는 소유자는 기본 거부한다. runtime 소스 revision 검사에도 같은 정책을 적용한다.

이 gate를 통과한 sibling의 내용과 설정은 **신뢰한 운영자 입력**이다. guard는 revision을 고정하고 우발적인 drift를 감지하는 장치이지, 적대적 저장소나 악의적인 신뢰 소유자를 격리하는 sandbox가 아니다. 검사 중 다른 writer가 소유권·설정·attributes를 변경하지 않도록 운영한다. 운영자 SID는 비공개 runtime 설정에만 넣으며 저장소에 절대 기록하지 않는다. `trusted_repo_owners`를 wildcard나 검토하지 않은 identity로 넓히지 않는다.

기존 hardening은 유지한다. 실행 가능한 Git 설정 denylist와 include 거부, working/index/info attributes의 `filter=` 거부, `GIT_NO_LAZY_FETCH=1`, 명령별 `protocol.allow=never`, 최소 환경과 global/system config 차단을 적용한다. stdout/stderr 공유 4 MiB 상한과 30초 timeout, 초과 시 process-tree 종료, attribute 탐색 상한도 유지한다. submodule은 지원하지 않으며 gitlink·`.gitmodules`·Git metadata의 `modules` 디렉터리를 기본 거부한다. 이러한 방어를 신뢰 소유자에 대한 OS 격리 보장으로 확대 해석하지 않는다. revision/활성화 계약은 [ADR-0009](0009-workspace-revision-guard.md)에 기록한다.

## 대안

다중 사용자 서비스·분산 잠금·격리된 credential worker는 확장 대안이지만 현재 P0 계약보다 운영 복잡도가 크다.

## 결과

운영자는 다른 writer를 중단하고 lock/journal/head 문제를 확인한 후 복구한다. 방어 검사가 있다는 사실을 동시성 지원이나 로컬 침해 저항으로 표현하지 않는다.

## 재검토 조건

다중 운영자·스케줄러 중첩·비신뢰 플러그인·공유 PC 또는 지속적인 외부 Hub writer가 필요해지면 이 전제와 저장소/인증/조정 계층을 먼저 재설계한다.

## 근거

[Workspace guard](../../src/kapa_runtime/releases/workspace.py), [OS 소유권 정책](../../src/kapa_runtime/releases/owners.py), [설정](../../src/kapa_runtime/config.py), [단일 writer 장부](../../src/kapa_runtime/storage/state.py), [원격 검증](../../src/kapa_runtime/releases/hf.py), [파이프라인](../../src/kapa_runtime/pipeline.py), [운영 절차](../RUNBOOK.md).
