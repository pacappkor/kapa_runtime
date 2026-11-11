<!-- SPDX-License-Identifier: GPL-3.0-only -->
# 07. 설계 결정과 출처

## ADR 요약

| ID | 결정 | 이유 | 재검토 조건 |
|---|---|---|---|
| ADR-001 | GitHub를 법률 지식·코드 기준으로 유지 | 기존 원문·검토·의존성 계약 재사용 | 원본 관리 방식 변경 |
| ADR-002 | HF private를 검증 데이터 배포에 사용 | snapshot·대량 Parquet·평가셋 공유 | 용량·비용·계약·접근 지연 문제 |
| ADR-003 | DuckDB + SQLite로 시작 | 로컬 단일 사용자, 운영 부담 최소화 | 다중 writer·원격 권한·공간 분석 |
| ADR-004 | 기존 MCP와 외부 실행을 분리 | 기존 읽기/조판 범위를 유지 | 명시적인 계약 개편 및 회귀 검증 |
| ADR-005 | 멀티 레포 manifest로 활성화 | cross-repo 원자 업로드를 가정하지 않음 | 배포 트랜잭션 대안 검증 |
| ADR-006 | Ontology는 스키마·관계부터 | 기존 지식·그래프 활용, 추론 과장 방지 | 관계 쿼리 성능·표준 교환 요구 |
| ADR-007 | Memory와 공통 지식 분리 | 사건별 가정·고객 정보 혼입 방지 | 검토된 지식 편입 정책 변경 |
| ADR-008 | 키는 지정 loader만 읽음 | 프롬프트·로그·업로드로의 확산 방지 | OS/서버 Secret Manager 도입 |

이 결정은 현재 요청에 대한 제안이다. 새 repo·HF 리소스·배포를 생성한 기록이 아니다.

## 미확정 사항

| 항목 | 현재 상태 | 해결 시점 |
|---|---|---|
| HF namespace·대상 repo | 미확인 | 최초 HF 작업 전 |
| hf_token.txt 권한·유효성 | 존재만 확인 | credential 구현 후 bounded 점검 |
| data_gov_key.txt key format | 미확인 | 최초 API 점검 전 |
| 승인 API·운영 한도·만료 | 사용자 연결 완료 보고, 서비스별 미검증 | registry 초기화 |
| 저장·재배포 조건 | 서비스별 검토 필요 | 해당 데이터 외부 업로드 전 |
| 브이월드 인증 | vworld_key.txt 존재 확인, 값·권한·도메인 조건 미검증 | 관련 기능 구현 전 |
| R-ONE 인증 | rone_key.txt 존재 확인, 값·권한·통계표 접근 미검증 | 관련 기능 구현 전 |
| 주소 등 추가 API | 필요 기능·기존 승인 확인 후 부족분만 사용자 요청 | 08_AGENT_HANDOFF.md 참조 |
| 기존 lock과 현재 HEAD 호환 | 앞선 경고 존재, 이번 재검증 없음 | 통합 manifest 최초 생성 |
| 원격 서버·다중 사용자 | 현재 범위 외 | 별도 요구 시 |
| 갱신 주기·예산·보존 | 미정 | 소규모 실측 후 |

## 확인 근거

2026-11-10에 로컬 폴더 이름, 세 레포 README 앞부분, knowledge deps.lock 앞부분을 읽었다. 키 파일은 본문·길이·해시를 읽지 않았다. 앞선 대화에서 GitHub 연결을 통해 다섯 레포와 주요 문서를 읽었으며 연결 MCP에서 판본 불일치 경고를 관측했다. 로컬과 원격이 현재 같은 HEAD라는 검증은 이번에 수행하지 않았다.

## 공식 출처

아래 문서는 설계의 서비스 기능·이용 경계를 확인하기 위한 출처다. 특정 사건의 법적 근거를 조사·검증한 목록이 아니다. 웹 문서는 변경 가능하므로 구현 때 SDK 버전·명세·요금제를 다시 확인하고 lock/fixture로 고정한다.

| 출처 | 확인 또는 사용할 내용 |
|---|---|
| [HF DuckDB](https://huggingface.co/docs/hub/en/datasets-duckdb) | HF 데이터의 SQL 조회·인증 연계 |
| [HF Upload](https://huggingface.co/docs/huggingface_hub/en/guides/upload) | dataset upload·commit·대량 업로드 |
| [HF Tokens](https://huggingface.co/docs/hub/en/security-tokens) | read/write 및 fine-grained 범위 |
| [HF Storage limits](https://huggingface.co/docs/hub/en/storage-limits) | private 용량·요금·파일 운영 권고 |
| [HF Spaces storage](https://huggingface.co/docs/hub/en/spaces-storage) | 임시 디스크·지속 저장 경계 |
| [GitHub Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets) | Actions 비밀 전달·권한 |
| [GitHub Variables](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-variables) | 조직 private 접근의 요금제 제약 |
| [GitHub OIDC](https://docs.github.com/en/actions/concepts/security/openid-connect) | 배포 대상의 단기 인증 |
| [아파트 매매](https://www.data.go.kr/data/15126469/openapi.do) | 지역·계약월 조회, 공개 범위 |
| [아파트 전월세 공식 metadata](https://www.data.go.kr/catalog/15126474/openapi.json) | 계약월 조회, 동/호 미공개 |
| [토지 거래 공식 metadata](https://www.data.go.kr/catalog/15126466/openapi.json) | 지번 일부 공개 |
| [건축HUB metadata](https://www.data.go.kr/catalog/15134735/openapi.json) | 공부 필드·제공 형식·PK 변경 안내 |
| [행위제한 metadata](https://www.data.go.kr/catalog/15058410/openapi.json) | 지역·지구·행위별 규제 |
| [규제 법령 metadata](https://www.data.go.kr/catalog/15057174/openapi.json) | 법령·조례 조항 연결 |
| [토지이용계획 metadata](https://www.data.go.kr/catalog/15123973/openapi.json) | LINK 형태 제공 |
| [토지이음 개방 목록](https://www.eum.go.kr/web/op/sv/svItemList.jsp) | API·CSV·SHP·PDF의 구분 |
| [국가법령정보 공동활용](https://open.law.go.kr/LSO/information/guide.do) | 법령·연혁·판례·해석례 연계 |
| [R-ONE 개발가이드](https://www.reb.or.kr/r-one/portal/openapi/openApiDevPage.do) | 통계 API |

실제 API 호출이 없으므로 엔드포인트 정상성·승인·레코드 품질은 이 문서가 입증하지 않는다. HF SDK와 MCP SDK 버전은 새 런타임 구현 시 정하며 기존 MCP lock을 임의 수정하지 않는다.

## 후속 키 참조 업데이트 (2026-11-10)

사용자의 지시에 따라 rone_key.txt와 vworld_key.txt 파일명·존재만 확인하고 설계의 자격증명 경로에 추가했다. 키 값·길이·해시·유효성은 조회하지 않았다. 추가 API 신청 요청은 08_AGENT_HANDOFF.md에 인계했다.
