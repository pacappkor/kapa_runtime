<!-- SPDX-License-Identifier: GPL-3.0-only -->
# kapa_runtime

[![visitors](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/pacappkor/kapa_runtime/traffic/.traffic/badge-views.json)](https://github.com/pacappkor/kapa_runtime)
[![clones](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/pacappkor/kapa_runtime/traffic/.traffic/badge-clones.json)](https://github.com/pacappkor/kapa_runtime)

KAPA 공공데이터 수집·정규화·데이터 릴리스·업무 상태 런타임. 코드는 공개하며 운영 데이터와 자격증명은 저장소 밖에 둔다.

설계: [원본 설계안 v0.1](docs/design/README.md)(01–08)과 [실제 구현의 결정·차이(ADR)](docs/adr/README.md). 운영은 [RUNBOOK](docs/RUNBOOK.md)을 따른다.

2026-11-11 공개 정책에 따라 이 저장소는 공개 코드 경계이며, 로컬 운영 데이터는 저장소 밖의 `data_root`에 둔다. HF `pacappkor/kapa-market-data`는 공개 market 데이터셋이고 `pacappkor/kapa-market-data-staging`은 비공개 검증용이다. HF 설정 기본값은 여전히 private이며, 검토된 `market` 역할만 명시적 public 설정과 서비스별 이용조건 검토를 허용한다. 실제 가시성과 설정이 다르면 자동 변경 없이 거부한다. knowledge-index, evals, property, matters는 private을 유지하며 raw·로그·자격증명은 공개하지 않는다.

## 이용 시 주의 및 보안

공개 코드와 데이터는 법적 정확성·완전성·최신성을 보장하지 않으며 법률 검증이나 공식 해석을 대신하지 않는다. 원자료 이용·재배포는 [공공데이터포털(data.go.kr)](https://www.data.go.kr/)의 해당 서비스 및 제공기관 이용조건을 따른다. 코드 라이선스가 원자료 이용조건을 대체하지 않는다. 취약점은 [SECURITY.md](SECURITY.md)의 비공개 신고 절차를 따른다.

## 로컬 데이터 조회 MCP

`uv sync --locked --extra mcp` 후 `KAPA_RUNTIME_CONFIG`에 명시적 JSON 설정 경로를 제공하고
`kapa-runtime-mcp` (또는 `python -I -B -m kapa_runtime.mcp.server`)를 실행한다.
stdio 전용이며 `list_data_capabilities(service_id=null)`와
`search_transactions(filters={}, limit=100, cursor=null)` 두 읽기 도구만 제공한다.
ACTIVE manifest와 로컬 ledger에 결속된 snapshot만 읽고 자격증명·HF SDK를 불러오거나
네트워크 조회·수집·publish·activate하지 않는다. 토큰이 없는 전용 설정을 권장한다.
등록 예시, 허용 필터, 결과 상태는 [RUNBOOK](docs/RUNBOOK.md#로컬-조회-mcp-stdio)을 참고한다.
SDK 프로토콜 테스트: `uv run --locked --extra test --extra mcp python -I -B -m pytest -p no:cacheprovider -q tests/test_data_mcp.py`.
