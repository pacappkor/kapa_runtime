<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0009: Workspace revision 고정과 활성화 guard

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `1f3f877`

## 배경

runtime 데이터만 고정해도 함께 사용하는 knowledge와 skills checkout이 달라지면 게시 당시의 입력 조합을 재현할 수 없다. 반대로 조회 시마다 게시자의 workspace를 요구하면 이미 검증한 로컬 snapshot을 읽는 MCP가 운영 환경에 종속된다. Codex 검토의 수렴점은 소유권으로 승인한 입력의 revision 고정과 우발적 drift 탐지이며, 적대적 저장소 sandboxing이 아니다. 이 범위를 명시하고 확인할 수 없는 상태는 기본 거부(default-deny)한다.

## 결정

- Build는 manifest의 `github_revisions`에 `pacappkor/kapa_runtime`과 설정된 sibling을 완전한 `owner/repo` ID 및 40자리 전체 commit SHA로 기록한다. `runtime` 같은 alias나 움직이는 branch 이름으로 대체하지 않는다. 지원 sibling은 `kapa_edu_1`, `kapa_edu_2`, `kapa_skills`, `kapa_knowledge`, `kapa_mcp`이며 기본 필수 입력은 knowledge와 skills이다.
- 존재하는 모든 설정 sibling은 clean Git checkout이어야 한다. 실제로 없는 optional sibling만 `null`과 `workspace_repository_unavailable` warning으로 기록한다. 필수 sibling 부재, 존재하지만 읽을 수 없거나 Git 저장소가 아닌 입력, dirty checkout은 build를 거부한다. optional이라는 이유로 손상된 입력을 누락 처리하지 않는다.
- Build에서 knowledge가 참여하면 working tree의 `deps.lock`에 있는 `kapa_skills.commit`과 `kapa_skills.vendor.commit` 모두 기록된 skills HEAD와 같아야 한다. 없거나 잘못된 lock 또는 불일치는 `workspace_dependency_mismatch`로 거부한다.
- Build의 provenance 수집과 별도로, 게시된 manifest에 대한 workspace 일치 검사는 관리 작업인 activate/rollback 시점에만 수행한다. 현재 설정의 각 `required_repos` checkout은 clean 상태이고 대상 manifest의 고정 HEAD와 정확히 같아야 한다. object store에 해당 commit이 있다는 것만으로는 충분하지 않다. knowledge가 필수일 때는 검증된 knowledge checkout의 lock도 기록된 skills revision과 대조한다. 불일치·누락은 `workspace_revision_mismatch`로 거부하고 active pointer를 유지한다. 기존 원격 게시 검증도 계속 적용한다.
- MCP 조회 경로는 기록된 manifest, publication digest와 로컬 데이터를 검증한다. workspace checkout이나 `deps.lock`을 조회하지 않고 Git도 실행하지 않는다. 조회 머신에 게시자의 checkout 배치나 workspace 설정을 재현하도록 요구하지 않는다.
- Guard는 offline/read-only이며 fetch, checkout, global config 변경이나 입력 저장소 수정을 하지 않는다. submodule은 초기화 여부와 무관하게 지원하지 않는다. index gitlink, index 또는 디스크의 `.gitmodules`, Git metadata의 `modules` 디렉터리를 attribute 탐색 전에 거부하고 child 저장소를 검사·실행하지 않는다. runtime 소스에도 같은 거부 정책을 적용한다.
- 소유권 gate와 hardening의 보장 범위는 [ADR-0008](0008-single-operator-threat-model.md)을 따른다. 필수 입력을 줄이거나 manifest를 수정해 불일치를 우회하지 않는다. 명시적인 빈 `required_repos`는 해당 입력을 소비하지 않는 격리 배포에만 사용한다.

## 대안

최신 branch 자동 선택이나 누락 입력의 자동 fetch/checkout은 게시 당시 조합을 조용히 바꾸고 read-only 경계를 깨므로 채택하지 않는다. MCP가 매 조회마다 workspace를 검사하는 방식은 관리 검증과 조회 계약을 결합하므로 채택하지 않는다. submodule 재귀 지원과 악성 신뢰 소유자에 대한 추가 sandbox 방어는 현재 경계 밖이며, 검토를 무한히 확장하는 대신 지원하지 않는 입력을 기본 거부하는 범위로 수렴한다.

## 결과

운영자는 manifest의 고정 조합으로 checkout하고 local 변경을 정리하거나 별도로 보존한 후 활성화를 재시도한다. 과거 manifest에 필수 pin이 없으면 자동으로 최신 revision을 채우지 않는다. 조회는 검증된 snapshot만으로 가능하다.

SHA는 Git tracked tree를 식별할 뿐 ignored 파일 전체를 증명하지 않는다. ignored 입력은 별도 digest 계약이 필요하며 비밀은 추적하지 않는다. `deps.lock`을 의도적으로 ignore한 경우에도 skills pin 값은 검사하지만 그 파일 바이트가 knowledge HEAD로 증명되는 것은 아니다. 공개 market manifest의 private 저장소 이름·SHA 공개 범위는 [ADR-0002](0002-hf-visibility.md)를 따른다.

## 재검토 조건

지원 sibling 집합이나 lock 형식 변경, submodule 지원, 비신뢰 저장소 입력, 다중 운영자 또는 ignored 입력까지 포함하는 재현성 보장이 필요하면 소유권·고정 revision·조회 분리 계약을 함께 재검토한다.

## 근거

[Workspace guard](../../src/kapa_runtime/releases/workspace.py), [OS 소유권 정책](../../src/kapa_runtime/releases/owners.py), [파이프라인](../../src/kapa_runtime/pipeline.py), [설정](../../src/kapa_runtime/config.py), [운영 절차](../RUNBOOK.md)의 Multi-repository activation guard 및 로컬 조회 MCP 절.
