<!-- SPDX-License-Identifier: GPL-3.0-only -->
# kapa_runtime

KAPA 공공데이터 수집·정규화·비공개 데이터 릴리스·업무 상태 런타임(비공개).

설계: [원본 설계안 v0.1](docs/design/README.md)(01–08)과 [실제 구현의 결정·차이(ADR)](docs/adr/README.md). 운영은 [RUNBOOK](docs/RUNBOOK.md)을 따른다.

이 저장소와 HF 데이터셋은 기본 private이다. 사용자 정책에 따라 검토된 `market` 역할만 명시적 public 설정을 허용하며, 실제 가시성을 자동 변경하지 않는다. 나머지 역할은 private을 유지한다.

## 로컬 데이터 조회 MCP

`uv sync --locked --extra mcp` 후 `KAPA_RUNTIME_CONFIG`에 명시적 JSON 설정 경로를 제공하고
`kapa-runtime-mcp` (또는 `python -I -B -m kapa_runtime.mcp.server`)를 실행한다.
stdio 전용이며 `list_data_capabilities(service_id=null)`와
`search_transactions(filters={}, limit=100, cursor=null)` 두 읽기 도구만 제공한다.
ACTIVE manifest와 로컬 ledger에 결속된 snapshot만 읽고 자격증명·HF SDK를 불러오거나
네트워크 조회·수집·publish·activate하지 않는다. 토큰이 없는 전용 설정을 권장한다.
등록 예시, 허용 필터, 결과 상태는 [RUNBOOK](docs/RUNBOOK.md#로컬-조회-mcp-stdio)을 참고한다.
SDK 프로토콜 테스트: `uv run --locked --extra test --extra mcp python -I -B -m pytest -p no:cacheprovider -q tests/test_data_mcp.py`.
