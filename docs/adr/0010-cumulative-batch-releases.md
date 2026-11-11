<!-- SPDX-License-Identifier: GPL-3.0-only -->
# ADR-0010: 누적 batch release와 manifest shard 계약

상태: 계약 채택, 운영 통합 전 · 날짜: 2026-11-12 · 관련: issue #3

## 배경

한 지역·월 릴리스와 전체 파일 glob은 전국 누적 수집의 조회 단위가 될 수 없다. 기존 ADR-0002~0009의 digest, private staging, 단일 writer, workspace pin, fail-closed, tombstone 및 `legal_verified=false` 보장은 유지한다. manifest 버전과 Arrow 데이터 버전은 별개다.

## 결정

수집 observation → 배포 batch release → 조회 cumulative ACTIVE snapshot을 분리한다. pair P는 `(lawd_code, deal_ymd)`, partition K는 `(lawd_code, deal_ymd, service_id)`다. batch는 정렬·고유한 정확한 pair 집합을 선언하고 각 pair의 sale와 rent 완전 관측을 함께 교체한다. `S_new = S_parent ⊕ batch`: 같은 K는 오른쪽으로 교체하며 상속 partition의 producer와 근거를 보존한다. 정상 zero도 관측이며 재관측은 coverage를 늘리지 않는다. 전체 행 dedup을 하지 않고 관측 안의 multiplicity를 보존한다. 재관측에서 사라진 행은 취소 증거가 아니다.

새 경로는 `data/observations/<observation_id>/asset_type=apartment/transaction_type=.../lawd_code=.../contract_year=YYYY/contract_month=MM/part-NNNNN.parquet`다. 2단계의 opt-in `snapshot.write_once`는 동일 경로 다른 바이트를 거부한다. bootstrap은 검증된 기존 경로를 그대로 선택할 수 있다. raw는 로컬에 남는다.

논리 K 선택과 물리 경로 소유권은 다르다. 선택 해제는 tombstone이 아니다. 과거 관측은 retained inventory로 영구 보존하며, 기본 inventory에는 current 선택 파일과 metadata만 포함한다. staging/public 소유권 합집합 **이후** tombstone 적용을 유지하며 manifest 참조가 삭제 파일을 부활시키지 않는다. 복구 사본이 없으면 게시 중지, 제거는 명시적 withdrawal로 한다.

### Wire 계약

`kapa-runtime-release/2`는 v1 공통 근거 필드에 다음 필드를 추가한다. v1 canonical serialization은 변경하지 않고 알 수 없는 manifest 버전은 거부한다.

- `release_kind`: `batch|bootstrap`.
- `parent_snapshot`: 필수 `{release_id, digest}`. 자기 자신 참조 금지. 최초 이행도 신뢰된 legacy parent를 요구한다.
- `batch_scope.declared_pairs`: `{lawd_code, deal_ymd}`의 정렬·고유 목록, 1~60개.
- `selected_partitions`: K, `producer_release_id`, `origin_digest`, `observation_id`, `run_id`, timezone-aware `fetched_at`, `status(success|zero)`, `rows`, `parser_version`, `schema`, `files`, `previous_observation`, `evidence_digest`.
- `files` 항목: `{path, sha256, rows}`. partition rows 합계와 일치하며 zero에도 빈 Parquet 파일을 명시한다. 데이터 schema는 `kapa-runtime-data/2`만 허용한다.
- `origin_digest`: 현재 producer에는 null(자기 digest 순환 금지), 상속 producer에는 원본 manifest canonical digest. 이전 관측은 null 또는 `{observation_id, producer_release_id, digest}`다. 이 단계는 모양만 검증하며 신뢰 장부와 실제 origin 검증은 다음 단계 책임이다.
- `coverage`: `declared`, `observed`, `zero`, `not_collected` 각각 정확한 K 목록과 `complete` boolean. `zero ⊆ observed`, `not_collected = declared − observed`, `complete = not not_collected`. observed는 누적 선택 K와 같고 zero는 status와 같다. complete는 선언 집합의 완료일 뿐 전국 전체 완료가 아니다. 상속 observed는 현재 선언 밖에 있을 수 있다. 지역×월×서비스 곱집합 해석은 금지한다. 선언·관측·미수집은 완전 pair 단위이고 zero는 서비스별이다.
- `coverage_shards`: 정렬·고유한 `{path, sha256, entries}`. 경로는 정확히 `manifests/coverage/<sha256>.json`. root `files`는 metadata만, data는 selected partition의 file 목록에서 해석한다. shard 자체는 별도 hash-bound metadata이며 반환되는 snapshot data inventory에 중복 삽입하지 않는다.

Shard는 `kapa-runtime-coverage/1`, `license`, `selected_partitions`, `coverage`만 가진 평면 문서다. root와 shard의 K 목록은 합쳐서 검증하므로 중복 K·coverage를 거부한다. 각 shard 안에서도 pair와 coverage 관계가 완전해야 하므로 pair를 쪼개지 않는다. 시도·연도별 분할을 권장하지만 지리 grouping 자체는 이 단계의 형식 제약이 아니다. shard 안에서 다른 shard나 parent를 따라갈 수 없다(순환 참조 불가능). `entries`는 selected partition 수와 네 coverage 목록 길이의 합이다.

Root inline 데이터도 하나의 block으로 취급한다. 각 block은 병합 **전에** selected K = observed K, 완전 sale+rent pair, zero K = rows가 0인 선택 partition K (status도 zero), `not_collected = declared − observed`, `complete = not not_collected`를 독립 검증한다. batch 전체 포함·producer/origin 결속·파일 중복 검사는 병합 후 기존 전역 검증을 유지한다.

Pair의 소유 block은 `declared ∪ observed`로 정하며 root와 모든 shard 사이에서 유일해야 한다. 선언된 관측 pair의 declared/observed/selected/zero는 같은 block에 둔다. 아직 미수집인 pair는 declared와 not_collected를 같은 block에 두며 partition은 없다. 선언 밖 상속 observed pair는 허용하지만 다른 block에 그 pair의 declared/not_collected를 둘 수 없다. 따라서 coverage만 가진 미수집 block은 가능하고, 다른 block의 관측으로 local gap을 메우거나 pair 기록을 분산하는 방식은 금지한다. 전역 합집합만 검증하는 대안은 서로 바뀐 shard 관계를 숨기므로 채택하지 않는다.

상한은 root 및 **각 shard 16 MiB**, shard 최대 1,024개, shard당 합계 4,096 entries, 전체 root+shard 합계 250,000 entries다. 파일/목록도 250,000개 상한을 적용한다. 무한 metadata 한도 확대 대신 향후 측정 후 계약 개정한다. resolver는 loader가 반환한 bytes 길이·SHA-256·entry count를 검사하고 명시된 경로만 한 번씩 요청한다. loader는 안전한 파일 경계와 고정 revision에서 bounded read를 보장해야 한다. glob, 네트워크, parent 자동 조회는 없다.

`validate_manifest`는 root 구조 검증이다. shard가 있으면 완전한 cross-shard 검증은 `resolve_snapshot`/`resolve_snapshot_inventory` 성공을 요구한다. v1 inventory는 기존 files 그대로다. v2 inventory는 선택 current data + root metadata를 path 순서로 반환한다. `resolve_snapshot`의 펼친 결과는 작업용 view이지 다시 직렬화할 root가 아니다.

Canonical digest는 기존과 같은 UTF-8, 정렬 key, compact separators, non-ASCII 보존, finite JSON, 마지막 LF에 SHA-256이다. shard SHA는 실제 저장 bytes를 결속한다. candidate 자체 hash/commit 필드는 금지한다. parent/origin digest는 자기 hash가 아니다. 최종 publication 결속은 외부 장부 및 기존 revision 필드에서 처리하며 final-envelope 추가는 다음 통합 단계에서 명시적으로 설계한다. JSON examples의 SPDX는 JSON 주석 대신 license 문자열로 표기한다.

### 소유자 결정 (2026-11-12)

- 서울 우선, 이후 전국; 최신 월부터 역순.
- 두 서비스가 모두 제공하는 기간에서 완전 sale+rent pair만 수집한다. 정확한 earliest는 제공 범위 확인 전 미확정이다.
- 서비스당 일일 운영 예산 9,000 requests.
- batch 상한 60 pairs / 변경 512 MiB. byte cap의 실행 enforcement는 수집·게시 단계 책임이다.
- 수동 smoke를 먼저 수행한 뒤 매일 자동 publish+activate. 오류 시 중지한다.

### 3a 계획 계층 (issue #5, 2026-11-12)

`planning.py`는 관측 pair와 관측일을 입력받는 순수 계획 계층이다. 실행·DB·quota 예약·pipeline 통합은 후속 단계다. sale/rent 잠정 범위의 교집합에서만 계획하고, 서비스별 예산의 최솟값으로 상한을 잡는다. refresh 20%와 audit 5%를 우선 확보하며 미사용 share는 backfill로 이관한다. refresh backlog가 share를 넘으면 SLA shortfall을 명시한다. 현재월 daily 지역 순환, 직전 1~3개월 7일, 직전 4~12개월 calendar-month anniversary(월말 clamp), 더 오래된 관측 pair는 순환 audit이다. refresh는 최신월 우선으로 월별 미관측·오래된 관측일·코드 순서, audit은 오래된 관측일 우선(동률은 최신월·코드)으로 선택한다. 성공한 관측일의 coverage 반영이 cursor를 전진시키며 예산 변화에도 공정성을 유지한다. 현재월 269지역을 기본 12 slots/day로 순환하는 데 ceil(269/12) = 23일이 필요하므로 daily cadence가 전국 daily SLA는 아니다. 서울 전 기간 backfill 뒤 전국 나머지, backfill 각 단계는 최신월·코드 오름차순을 유지한다. 상세 cadence와 입력 계약은 RUNBOOK의 수집 계획(3a)을 따른다.

code.go.kr 전체자료의 active 10자리 코드에서 5자리 prefix를 생성하고 시도 집계 `xx000`은 제외한다. provenance에 다운로드 URL·날짜·원본 ZIP 및 내부 파일 SHA-256·생성 규칙·count를 결속한다. 결정적 로컬 generator를 제공하고 대형 원본은 커밋하지 않는다. 재생성 시 `tools/build_lawd_codes.py <raw-file> --output-dir <directory> --download-date 2026-11-12`처럼 실제 다운로드 날짜를 필수 YYYY-MM-DD 인자로 명시한다(커밋된 원본의 날짜는 2026-11-12). 역사적 폐지·분할 코드 처리와 RTMS별 지역 의미 확인은 미해결이며 현재 표가 모든 과거 지역 coverage를 보장하지 않는다.

## 대안과 결과

단일 무한 manifest와 전역 glob은 metadata 크기 및 archive 중복 집계 문제로 거부한다. 매 batch 전체 복사도 비용 때문에 채택하지 않는다. flat hash-bound shard는 명시적 inventory와 제한된 검증 비용을 제공하지만 shard 조립·전역 중복 검사와 provenance 검증이 필요하다. 기존 v1 reader와 pipeline은 계속 동작한다.

## 비목표와 다음 단계

2단계는 opt-in snapshot 조립, ledger publication digest 기반 origin 검증, parent 비교, write-once, SQLite schema 3(parent/batch members/partition evidence), 전체 선택 inventory 검증 및 v2 activation/rollback 지원을 추가한다. 기본 v1 CLI는 유지한다. v2 게시 orchestration/final envelope와 게시 직전 check_parent 연결은 미완료이며 v1 publish에 v2를 전달하지 않는다. HF delta/spool, quota scheduler, CLI, query/MCP, bootstrap 실행은 후속 단계다. 실제 수집·네트워크·게시도 하지 않는다.

현재 조립 API는 parent digest 비교와 published-but-not-active 차단을 제공한다. 다음 통합 작업은 v2 게시 직전에 이 guard를 다시 호출하고 end-to-end fake-HF 검증을 추가하는 것이다. schema 3 migration은 첫 누적 snapshot 기록 때만 실행하여 기본 v1 장부 동작을 유지한다. 미해결 journal·commit 결과 불명·head/visibility drift는 전역 중지 사유다. local rollback과 public current rollback은 별개이며 기존 head 제공·workspace guard를 유지한다. 이후 quota/jobs, delta/spool, manifest-aware query, migration과 수동 smoke 순으로 통합한다.

### 3b 실행 계층 (issue #5)

`accumulation.py`는 기존 StateStore writer lock과 runs 기반 Seoul 일일 quota를 재사용한다.
collection_jobs/work_items/attempts는 명시적 schema 4 migration으로만 생성한다. plan/reference/
policy/parser binding, 각 시도의 observation FK, page/hash 및 terminal state를 기록한다.
두 서비스 예약은 하나의 SQLite transaction이며 9,000/service/day와 낮은 기존 한도를 적용한다.
midnight 요청 전 재예약하며 성공 sibling은 replay 검증 후 재사용한다. 실패/partial 합산은 금지한다.
auth/schema/security/verification은 persistent blocked job으로 모든 새 job을 차단한다.

Immutable observation writer와 build_snapshot/check_parent를 이용해 로컬 누적 snapshot까지만
진행한다. 전체 계획을 하나의 batch(60 pair, 변경 Parquet 512 MiB)로 동결하며 cap 초과는
자동 분할 대신 fail-closed한다. 빌드 완료 job의 다음 lifecycle은 step 4가 담당한다.
legacy parent는 bootstrap evidence를 추정하지 않고 거부한다. 불완전 materialization 복구와
blocked 해제는 자동화하지 않는다. 실제 운영은 operator smoke와 step 4 게시 통합 전 금지한다.

LAWD city aggregate exclusion은 기본 true의 config flag다. active city 이름 prefix와 코드 앞
4자리가 일치하는 하위 gu가 있으면 끝자리 0의 city aggregate를 제외한다. committed 원본과
provenance는 그대로 보존하고 digest에 결속한다. MOLIT sigungu 의미에 대한 잠정 결정이며
실제 제공 범위·중복 방지 효과는 operator smoke로 검증해야 한다.
