<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0006: 데이터 스키마 v2와 기본 거부 extra 필드

상태: 채택(구현 기록) · 기록일: 2026-11-11 · 기준: `7e7b1f1`

## 배경

원본/초기 계약은 미지 필드를 `extra`에 보존했다. 공개 정규화 자료에는 미검토 필드가 유출될 수 있고 현재 parser로 과거 릴리스를 재생하면 정상 과거 파일도 불일치할 수 있다.

## 결정

새 후보는 `kapa-runtime-data/2`를 사용한다. `extra`는 서비스 registry의 `normalized_extra_fields` allowlist만 보존하고 나머지는 정규화에서 버리며 관측 warning에 `unknown_provider_fields_dropped:<count>`를 기록한다. 미지 필드는 로컬 sanitized raw에만 남으며 raw는 게시하지 않는다.

v1/v2 Arrow 필드 순서·타입·metadata를 버전별 고정 스키마로 검증하고 미지 버전·혼합 snapshot을 거부한다. 신뢰된 published/active 릴리스는 장부 digest에 결속된 manifest와 snapshot의 파일 집합·hash·rows·고정 스키마로 확인하며 현재 parser/registry로 재정규화하지 않는다. 게시 전 질의는 현재 parser로 raw 근거를 재검증한다. legacy v1 extra도 현재 비밀 검사를 면제하지 않는다.

## 대안

모든 미지 필드 보존, 미래 모델에서 과거 스키마 동적 생성, 게시 후 매번 최신 parser 재생은 각각 공개 범위 통제 또는 불변 릴리스 재현성을 해친다.

## 결과

새 provider 필드는 명시적 검토 전 공개에서 빠진다. 과거 자료의 의미를 몰래 바꾸지 않으며 새 정규화 결과는 새 릴리스로 만든다.

## 재검토 조건

새 필드의 공개 필요성, parser 의미 변경 또는 새 데이터 스키마가 생기면 allowlist·버전·마이그레이션을 함께 검토한다.

## 근거

[정규화](../../src/kapa_runtime/normalization/rtms.py), [서비스 registry](../../src/kapa_runtime/registry/services.json), [고정 스키마](../../src/kapa_runtime/storage/parquet.py), [질의 검증](../../src/kapa_runtime/pipeline.py). 검토 계기: `_orchestrator/findings/runtime-pr1-review-22.md`.
