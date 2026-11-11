<!-- SPDX-License-Identifier: GPL-3.0-only -->
# 04. 외부 연결과 자격증명

## 현재 상태

사용자는 API 연결과 키 파일 저장을 완료했다고 알렸다. `data_gov_key.txt`, `hf_token.txt`, `rone_key.txt`, `vworld_key.txt`의 존재를 확인했으며 내용을 읽지 않았다. R-ONE·브이월드 키는 후속 사용자 요청으로 참조 대상에 추가했다. 실제 승인 서비스·키 표현형·토큰 범위·계정·유효기간은 미확인이다. 아래는 향후 구현 요구사항이며 이번 실행 결과가 아니다.

## 로컬 자격증명 계약

| 연결 | 우선 환경변수 | 명시적 로컬 fallback |
|---|---|---|
| 공공데이터 | DATA_GO_KR_SERVICE_KEY | <workspace>\data_gov_key.txt |
| HF 조회/업로드 | 전용 실행 단계의 HF_TOKEN | <workspace>\hf_token.txt |
| R-ONE | RONE_API_KEY | <workspace>\rone_key.txt |
| 브이월드 | VWORLD_API_KEY | <workspace>\vworld_key.txt |

환경변수가 정의되어 있으면 그것만 사용하고 비어 있거나 잘못됐을 때 파일로 조용히 fallback하지 않는다. 환경변수가 없고 설정에 허용된 정확한 파일 경로가 있을 때만 전용 credential loader가 읽는다. 부모 폴더를 검색하거나 다른 token txt를 후보로 읽지 않는다. 위 네 파일만 이번 설계의 명시적 로컬 참조 대상이다. 다른 키 파일을 탐색·사용하지 않는다.

구현 시 UTF-8 BOM과 끝 개행을 처리하고 키 1개만 허용한다. 여러 비어 있지 않은 줄이 있거나 라벨과 키가 섞인 형식이면 값을 출력하지 않고 설정 오류를 반환한다. 파일 경로의 링크·reparse point·ACL은 구현 시 확인한다. 이번에는 파일을 이동·복사·암호화·권한 변경하지 않았다.

credential loader 외에는 txt를 읽지 않는다. 키를 프롬프트·도구 인자·문서·manifest·URL 로그·HF 파일에 넣지 않는다. HTTP 디버그와 DuckDB SQL trace에도 키가 들어가지 않게 한다. 장기 운영은 OS/서버 비밀 저장소로 이전하고 경로 설정은 로컬 전용으로 유지한다.

공공데이터 키는 decoded 값을 내부 표준으로 권장한다. 현 txt가 encoding 키인지 추정하지 않는다. 명시적인 key_format 설정과 진단으로 확인하고 decoded 키를 HTTP 라이브러리가 한 번 인코딩하도록 한다. 값 자체에 대한 임의 반복 URL decode는 금지한다.

## 공공데이터 서비스 registry

서비스 ID·신청 페이지·endpoint 식별자·승인 상태·만료일·쿼터·지원 연월·응답 버전·필수 인자·정상/오류 코드·저장/재배포 정책·마지막 연결 점검을 기록한다. 사용자 승인 완료 보고와 실제 연결 점검 결과를 별도 필드로 보존한다. 현재 examples는 모두 approval-unverified다.

| 우선 | dataset ID | 서비스 | 신청·명세 페이지 |
|---|---|---|---|
| P0 | 15126469 | 아파트 매매 일반 | https://www.data.go.kr/data/15126469/openapi.do |
| P0 | 15126474 | 아파트 전월세 | https://www.data.go.kr/data/15126474/openapi.do |
| P1 | 15126468 | 아파트 매매 상세 | https://www.data.go.kr/data/15126468/openapi.do |
| P1 | 15126466 | 토지 매매 | https://www.data.go.kr/data/15126466/openapi.do |
| P1 | 15134735 | 건축HUB 건축물대장정보 | https://www.data.go.kr/data/15134735/openapi.do |
| P1 | 15058410 | 토지이용규제 행위제한 | https://www.data.go.kr/data/15058410/openapi.do |
| P1 | 15057174 | 토지이용규제 법령정보 | https://www.data.go.kr/data/15057174/openapi.do |
| P1 | 15123973 | 토지이용계획 WMS/WFS/속성 | https://www.data.go.kr/data/15123973/openapi.do |
| P2 | 15126467 / 15126473 | 연립다세대 매매 / 전월세 | 동일 dataset ID의 data.go.kr 상세 |
| P2 | 15126465 / 15126472 | 단독다가구 매매 / 전월세 | 동일 dataset ID의 data.go.kr 상세 |
| P2 | 15126464 / 15126475 | 오피스텔 매매 / 전월세 | 동일 dataset ID의 data.go.kr 상세 |
| P2 | 15126463 | 상업업무용 매매 | https://www.data.go.kr/data/15126463/openapi.do |
| P2 | 15126470 | 공장·창고 매매 | https://www.data.go.kr/data/15126470/openapi.do |
| P3 | 15126471 | 아파트 분양권전매 | https://www.data.go.kr/data/15126471/openapi.do |

dataset ID는 신청·카탈로그 식별자이며 API URL의 path가 아니다. 토지이용계획 항목은 LINK 제공이라 실제 제공처의 인증·API를 따로 확인한다. 모든 항목이 동일 data.go.kr 키로 처리된다고 가정하지 않는다. 기술문서의 실제 오퍼레이션·필드·한도는 구현 때 고정해 계약 fixture로 만든다.

브이월드와 R-ONE의 키 파일은 준비되어 있다. 이미 준비된 키를 사용자에게 다시 요청하지 말고 전용 loader로 연결한 뒤 권한·도메인 조건·통계표/데이터 접근·호출 한도를 확인한다. 주소 API와 법령 공동활용 등 추가 연결은 기존 승인 범위를 먼저 확인하고 실제로 부족한 항목만 요청한다. 키 파일 존재만으로 연결 성공을 표시하지 않는다. 특히 필지 지정·공시지가·표준지 정보의 이용 범위와 과거 연도 제공 여부를 기능별로 확인한다.

## API 어댑터 요구사항

서비스별 허용 host/path·method를 코드에 고정하고 사용자가 URL을 주는 범용 프록시를 만들지 않는다. HTTPS, 연결/응답 timeout, 최대 응답 크기·페이지·조회월·레코드 수, 요청별 실행 예산을 둔다. 리디렉션으로 키가 다른 host로 전송되지 않게 한다. XML은 외부 엔티티·DTD를 비활성화한다.

페이지 누락·반복 페이지·total 변경·에러 봉투를 검사한다. 재시도는 transient 오류와 429/5xx 등 확인된 경우만 제한적으로 적용하고 backoff·jitter·최대 시도·Retry-After를 따른다. 인증·미승인·인자 오류는 무한 재시도하지 않는다. 실제 서비스 규약에 맞춰 오류를 분류한다.

실거래 쿼리는 보통 지역코드·계약년월 기준이다. 먼저 한 지역·한 월로 전 페이지 검증을 한다. 쿼터는 registry에서 서비스별 관리하며 빈도가 높은 최신 기간 재조회와 과거 기간 대조는 별도 예산으로 실행한다. 구체 주기는 실측 후 결정한다.

## HF 연결

1. 사용자가 의도한 계정·조직 namespace와 토큰 접근 범위를 확인한다. GitHub pacappkor와 자동 동일시하지 않는다.
2. 조회·업로드 역할을 분리한다. 현재 hf_token.txt 하나를 양 역할에 써도 된다고 가정하지 않는다.
3. 대상 repo_type=dataset, private=true를 확인한다. public으로 전환하거나 권한을 넓히는 자동 복구를 하지 않는다.
4. 전용 staging의 명시적 파일 목록만 업로드한다. KAPA_edu 전체·design 전체·코드 레포 전체 업로드를 금지한다.
5. 파일 목록·비밀 제거·허용 필드·라이선스·품질을 검사한 뒤 SDK로 commit한다. upload ignore 설정만을 비밀 방지 수단으로 삼지 않는다.
6. 대용량 업로드가 여러 commit이면 전체 완료 후 최종 SHA·manifest·행 수를 검증한다. 업로드 중간 상태를 active로 노출하지 않는다.
7. 조회는 고정 commit SHA에서 필요한 파일만 내려받아 해시 확인 후 DuckDB로 읽는다. 최초 구현은 private HTTP 토큰을 SQL에 끼우는 방식보다 이 로컬 캐시 경로를 우선한다.

## Actions Secrets와 서버

제안 secret: DATA_GO_KR_SERVICE_KEY, HF_TOKEN_WRITE, HF_TOKEN_READ, RONE_API_KEY, VWORLD_API_KEY. LAW_OC 등은 필요한 별도 단계에서만 전달한다. 키 파일을 Actions artifact로 올려 전달하지 않는다. GitHub secret 값은 로컬 MCP가 회수해 사용하는 저장소가 아니다.

- PR CI: 비밀 없이 fixture와 오프라인 계약 검증.
- 수집 workflow: 기본 브랜치의 신뢰 코드, 최소 API 키·HF write만 단계별 전달.
- 릴리스 활성화: 데이터 검증 후 별도 단계, 모델·수집 단계와 권한 분리.
- multi-repo checkout: 범위를 제한한 GitHub App 등 별도 자격증명. 기본 GITHUB_TOKEN으로 다른 private repo 접근을 가정하지 않는다.
- workflow 변경 권한을 비밀 접근의 신뢰 경계로 본다. PR 코드를 privileged pull_request_target에서 실행하지 않는다.
- 조직 Secrets의 private 레포 지원과 environment 보호 기능은 요금제에서 확인한다.
- 서버 실행: 서버 비밀 저장소/환경변수 주입. OIDC는 배포 대상 인증에 사용하며 data.go.kr 키를 대체하지 않는다.

예약 실행은 아직 생성하지 않는다. 향후 갱신 승인 범위·호출 예산·검토 정책을 정한 뒤 기본 브랜치 workflow로 추가한다. 필요하다면 수동 실행부터 시작한다.

## 추가 API 요청 인계

[08_AGENT_HANDOFF.md](08_AGENT_HANDOFF.md)의 요청 규칙과 대장을 따른다. 이번 업데이트는 키 파일 참조만 추가하며 값을 읽거나 연결을 시험하지 않았다.
