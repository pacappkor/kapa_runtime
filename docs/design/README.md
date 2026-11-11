<!-- SPDX-License-Identifier: GPL-3.0-only -->
원본 설계안 v0.1 (2026-11-10) 사본; 실제 구현과의 차이는 docs/adr 참조

> 역사적 private-only 문구는 현재 코드 공개 정책이 아니다. 2026-11-11 정책은 공개 코드, 저장소 밖 로컬 운영 데이터, HF public market과 private staging을 구분한다. 현재 경계는 [ADR-0001](../adr/0001-repository-boundaries.md)·[ADR-0002](../adr/0002-hf-visibility.md) 및 [RUNBOOK](../RUNBOOK.md)을 따른다.

# KAPA 공공데이터·Hugging Face 시스템 설계안

작성일: 2026-11-10 (Asia/Seoul)  
상태: 구현 전 설계 초안 v0.1  
대상 폴더: `<workspace>`

## 핵심 결정

GitHub는 지식과 코드의 기준 저장소, Hugging Face Private Datasets는 검증한 데이터의 버전 배포 저장소, DuckDB는 거래·근거 데이터 조회 엔진, SQLite는 단일 사용자 업무 상태 저장소로 사용한다. 동시 쓰기·다중 사용자·공간 분석이 요구되면 PostgreSQL/PostGIS로 확장한다.

기존 `kapa_mcp`의 저장 자료 조회·근거 묶음·조판 계약은 유지한다. 외부 조회, 수집, 데이터 릴리스와 업무 상태 관리는 신규 비공개 `kapa_runtime`에 둔다. 이번 산출물은 설계 문서와 비밀 없는 설정 예시이며 실제 런타임이 아니다.

## 읽는 순서

| 문서 | 내용 |
|---|---|
| [01_ARCHITECTURE.md](01_ARCHITECTURE.md) | 저장소 책임, 배치 구조, 데이터 흐름, 배포 경계 |
| [02_KNOWLEDGE_MODEL.md](02_KNOWLEDGE_MODEL.md) | Wiki·Ontology·Graph·Skills·Memory 등 12개 개념의 적용 |
| [03_DATA_CONTRACTS.md](03_DATA_CONTRACTS.md) | 관측·거래·근거·릴리스 스키마, 정정·취소·시간 처리 |
| [04_CONNECTORS_AND_SECRETS.md](04_CONNECTORS_AND_SECRETS.md) | 공공 API, HF, 로컬 키 파일, Actions Secrets, 동기화 |
| [05_WORKFLOWS_AND_MCP.md](05_WORKFLOWS_AND_MCP.md) | 조회·갱신·평가 상태 머신, 제안 MCP 계약 |
| [06_IMPLEMENTATION_AND_EVAL.md](06_IMPLEMENTATION_AND_EVAL.md) | 단계별 구현 백로그, 인수 기준, 검증과 운영 |
| [07_DECISIONS_AND_SOURCES.md](07_DECISIONS_AND_SOURCES.md) | 설계 결정, 미확정 사항, 공식 출처 |
| [examples/runtime.config.example.json](examples/runtime.config.example.json) | 로컬 배치·키 파일 경로 예시 |
| [examples/services.example.json](examples/services.example.json) | 서비스 목록·승인 확인 상태 예시 |
| [examples/release-manifest.example.json](examples/release-manifest.example.json) | 여러 저장소와 HF 커밋을 연결하는 미완성 manifest |

## 후속 에이전트 인계

구현 전에 [08_AGENT_HANDOFF.md](08_AGENT_HANDOFF.md)를 읽는다. R-ONE은 rone_key.txt, 브이월드는 vworld_key.txt가 준비되어 있다. 파일 존재만 확인했으며 키 값·권한·유효성은 검증하지 않았다. 추가 API는 실제 기능 구현에 필요할 때 사용자에게 신청·권한 설정을 요청하고 독립 작업은 계속한다.

## 이번에 확인한 사실과 미확인 범위

- 로컬에 `kapa_edu_1`, `kapa_edu_2`, `kapa_skills`, `kapa_knowledge`, `kapa_mcp`, `_kapa_jobs`, `_orchestrator`가 있다.
- `data_gov_key.txt`, `hf_token.txt`는 파일명과 존재만 확인했다. 내용·길이·해시·권한·유효성은 조회하지 않았다.
- 사용자는 공공 API 연결을 완료했다고 알렸다. 개별 서비스의 승인, 호출 가능 여부와 한도는 아직 검증하지 않았다.
- HF 계정·조직명, 토큰 권한, private 용량과 데이터셋 존재 여부는 확인하지 않았다. GitHub 조직명과 HF namespace가 같다고 가정하지 않는다.
- 로컬 README에서 원문/요약 분리, 판본 고정, 조회와 갱신 분리, 기존 MCP 범위를 확인했다.
- 로컬 `kapa_knowledge/deps.lock`은 `kapa_skills`의 `536356823d43e867d426ba5eb5c6407ca64199cd`를 가리킨다. 앞선 연결 MCP 조회에는 다른 체크아웃과의 불일치 경고가 있었다. 이번에는 Git HEAD·잠금 객체 가용성을 다시 검사하지 않았다.
- 키로 API 호출, HF 생성·업로드, GitHub Secret 등록, 레포 변경, 예약 실행·서버 배포는 수행하지 않았다.

## 최소 구현 범위

한 시군구·한 계약월의 아파트 매매·전월세를 수집해 비밀 제거 → 정규화 → Parquet → 로컬 DuckDB 조회까지 연결한다. 검증 후 사용자가 지정한 HF private namespace에 후보 데이터를 올리고 commit SHA를 고정한다. 이후 토지·건축물·규제와 업무 상태를 추가한다.

설계 예시의 `null`, `approval-unverified`, `ready=false`는 의도적인 미확정 값이다. 실제 승인·계정·커밋을 확인하기 전 운영 설정으로 사용할 수 없다.
