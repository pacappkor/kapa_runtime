<!-- SPDX-License-Identifier: GPL-3.0-only -->
# 03. 데이터 계약과 릴리스

이 문서는 논리 계약이다. 실행용 JSON Schema·DDL·마이그레이션은 구현 단계에서 작성한다. examples의 JSON은 형태를 보여 주는 미완성 예시다.

## 공통 규칙

UTF-8·명시적 schema_version, timezone이 있는 수집시각, 날짜와 시각의 구별, 원문 값 보존, null과 0의 구별을 적용한다. ID·지역코드는 문자열로 저장한다. NaN·Infinity·중복 JSON key는 거부한다. 금액은 정수 KRW 또는 Decimal로 계산하며 면적은 단위를 가진 Decimal로 처리한다.

서비스가 만원 단위로 주면 원래 값과 단위를 보존한 뒤 KRW로 변환한다. missing 값을 0원으로 바꾸지 않는다. 전용·대지·연면적·계약면적은 서로 다른 필드다. 시군구 조회 코드와 법정동 전체 코드는 구별한다.

## 관측 Observation

| 필드 | 계약 |
|---|---|
| observation_id / run_id | 관측·실행 식별자 |
| provider / service_id / dataset_id | 출처 및 서비스 registry 참조 |
| endpoint_id / request | 허용 endpoint와 키 제거된 인자만 |
| started_at / fetched_at | timezone 포함 |
| source_reference_date / published_at | 제공기관이 제공한 경우만 기록, 없으면 null |
| status | success / zero / partial / failure |
| pages_expected / pages_received / count | 전체 수집 검증, 미제공 total은 null |
| response_schema / parser_version | 파싱·변환 규칙 |
| sanitized_payload_sha256 | 비밀 제거 후 저장 봉투의 정규 바이트 해시 |
| warnings / coverage | 조회 기간·지역·누락·식별 한계 |

HTTP 200만으로 success를 판정하지 않는다. XML 오류 봉투·API 결과 코드·페이지·형식까지 확인한다. 정상 응답에서 0건임이 확인돼야 zero다. 부분 성공은 partial이며 전체 범위 근거로 쓰지 않는다. 리디렉션·요청 URL·traceback에 키가 저장되지 않도록 한다. 비밀 제거 전 응답을 디스크에 남기지 않는 것을 기본으로 한다.

## 거래·임대차 레코드

공통: observation_id, provider_record_id(제공 시), record_version_id, asset_type, transaction_type, contract_date, lawd_code, address_original, address_visibility, building_name, area_type, area_m2, source_price, source_price_unit, price_krw, floor, build_year, cancellation_status, cancellation_date, first_seen_at, last_seen_at, source_payload_ref.

임대차: deposit_krw, monthly_rent_krw, contract_start/end(제공 시), new_or_renewal(제공 시), previous_values(제공 시). 임대차의 보증금과 월세를 임의의 환산율로 합치지 않는다. 환산 계산은 별도 산식·적용률·근거가 있는 파생 결과다.

source가 제공하지 않는 값은 추정해서 채우지 않는다. 아파트 매매 일반·상세 API를 동일 거래의 독립 사례로 이중 집계하지 않는다. 대응이 확정되지 않으면 비교 후보로 남긴다.

## 식별·중복·정정

공개 거래의 주소 마스킹 때문에 모든 레코드에 PNU를 부여할 수 없다. match_status는 exact / candidate / unresolved, match_method와 evidence를 기록한다. 후보를 exact로 자동 승격하지 않는다. 거래 레코드 ID와 부동산 ID는 별개다.

공급자 안정 ID가 있으면 우선 사용한다. 없으면 정규화 필드 fingerprint와 원문 관측을 함께 보존한다. 같은 날짜·금액·면적·층이어도 서로 다른 거래일 수 있으므로 fingerprint 일치만으로 원본을 삭제하지 않는다. 공식 ID 없는 정정·취소 연결은 matching decision과 근거를 남기고 애매하면 quarantine한다.

관측 이력은 추가 기록하고 사용 가능한 current view는 별도로 만든다. 취소 거래는 원본 이력을 보존하면서 기본 사례 선택에서 제외하고 제외 사유를 표시한다. 나중 snapshot의 누락만으로 취소를 단정하지 않는다.

## 시간 모델

contract_date(거래 발생), source_reference_date(자료 기준), published_at(확인된 공표), fetched_at(관측), legal_effective_from/to(법령 시행), decision_as_of(검토 기준)를 구별한다. 과거 계약을 오늘 조회했다고 과거에 공개돼 있었다고 주장하지 않는다. 공표시각 미제공이면 historical-availability=unknown을 기록한다.

## 공부·규제

PropertyObservation: PNU/건물 식별자, 원주소, 표준주소, 지목·면적·용도·층·사용승인일 등 제공 필드, source time, 좌표·CRS(제공 시), match_status.

RegulationObservation: 지정 지역·지구·구역 코드와 명칭, 포함/저촉/접함 등 원문 구분, 고시 참조, 제한 대상 행위, 조건, 원문 조문 참조, 적용 시점과 확인 수준.

필지 지정 현황과 행위제한·법령 조회를 분리한다. 현재 필지 정보는 과거 지정 상태의 증거가 아니다. 건폐율·용적률 상한만으로 실제 건축 가능 규모를 확정하지 않는다. 조례·고시·지구단위계획·다른 법률·접도 등 필요한 조건을 별도로 확인한다.

## HF 파일 구성

```text
README.md
schemas/
manifests/release.json
data/asset_type=apartment/transaction_type=sale/lawd_code=11110/contract_year=2026/contract_month=10/part-00001.parquet
quality/summary.json
```

예시 지역·연월은 테스트 조건일 뿐 실제 수집 완료를 뜻하지 않는다. 작은 파일 과다 생성을 피하도록 최종 크기와 파티션 수를 측정한다. 원본 봉투와 상세 수집 로그의 HF 포함 여부는 이용 조건·크기·비밀 제거 검증 후 따로 결정한다.

## 릴리스 manifest

release_id, state, created_at, source_runs, GitHub repo별 commit, HF repo별 commit SHA, schema/parser 버전, 파일별 SHA-256·행 수, 기간·지역 coverage, quality report, unresolved_gaps를 저장한다.

여러 HF 레포와 GitHub를 가로지르는 원자적 commit은 가정하지 않는다. 각 업로드의 commit SHA와 파일을 검증한 뒤 runtime의 하나의 manifest를 active로 교체한다. SQLite transaction 또는 검증된 파일 원자 교체를 사용한다. 실패한 후보는 active로 올리지 않으며 이전 manifest를 유지한다. 독자는 main/latest 대신 manifest에 지정된 commit을 사용한다.

사건 근거 묶음은 GitHub/HF revision, 관측·파일 해시, 인용 위치, 계산 버전·입력·가정·미확인 범위를 고정한다. 기존 MCP evidence bundle이 새 외부 관측을 지원한다고 가정하지 않고 별도 matter bundle 계약을 구현한다.
