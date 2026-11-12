<!-- SPDX-License-Identifier: GPL-3.0-only -->
# P0 운영 절차

## GitHub 공개 전 설정 확인

관리자는 GitHub private vulnerability reporting을 활성화하고 [SECURITY.md](../SECURITY.md)의 비공개 신고 경로를 확인한다. `.github/workflows/traffic.yml`은 조직 템플릿을 그대로 사용하며 기본 브랜치 `main`에 반영된 뒤 매일 실행된다. 조직 reusable workflow는 `main`을 별도 `traffic` 집계 브랜치에 동기화하고 방문·clone 집계를 기록한다. 관리자는 해당 저장소에 필요한 권한의 `TRAFFIC_TOKEN` Actions secret을 제공해야 한다. 토큰 값은 코드·문서·로그에 넣지 않는다. README 배지는 첫 집계 전에는 표시되지 않을 수 있다. 코드 변경만으로 이 GitHub 설정이나 실제 가시성이 바뀌지는 않는다.

## 누적 snapshot 개발 API (2단계)

기본 v1 CLI 경로는 유지한다. `releases.snapshot.build_snapshot(..., enabled=True)`만 누적 조립을 허용하며 StateStore writer lock 안에서 ACTIVE parent digest와 published-but-not-active/journal 차단을 확인한다. 게시 어댑터는 커밋 직전에 `check_parent`를 다시 호출해야 한다. 현재 v1 publish CLI에 v2를 전달하지 않는다. v2 publication orchestration/final envelope는 아직 통합하지 않았다.

`assemble_snapshot`의 trusted digest map은 사용자 manifest의 자기 주장 대신 장부 published digest에서만 구성한다. legacy bootstrap 순수 API는 검증된 partition evidence를 명시적으로 요구한다. `write_once`는 관측 경로 충돌을 거부하며 실패한 부분 파일은 자동 덮어쓰지 않는다. `materialize(..., enabled=True)`는 선택 inventory를 새 디렉터리에 설치하고 검증한다. 실패 디렉터리는 자동 재사용하지 않고 운영자가 원인을 조사한다.

신뢰된 v2 published manifest에 대해 Python `pipeline.activate/rollback(..., cumulative=True)`는 전체 선택 inventory, 고정 remote SHA, 현재 head 제공, workspace pin과 tombstone을 확인한 뒤 기존 atomic pointer 교체를 사용한다. 새 snapshot이 기존 파일을 선택 해제해도 archive 삭제가 아니다. 복구 가능한 동일 digest 사본이 없으면 observation 보존을 포기하지 않고 중지한다. SQLite schema 3은 parent, batch members, partition evidence만 추가한다. scheduler/jobs/quota, HF delta/spool, query와 bootstrap 명령은 후속 단계다.

## 사전 조건

Python 3.12와 `uv sync --locked --extra test`가 필요하다. 실제 호출은 운영자만 수행한다. Git 작업 트리가 깨끗한 커밋에서 테스트한 뒤 build한다. 자격증명은 설정의 상대 경로 또는 환경변수로 제공하고 출력·커밋하지 않는다. **HF 기본값은 private이다. 사용자 정책(2026-11-10)에 따라 market 역할만 명시적 public 설정이 가능하다.** GitHub 코드는 공개 대상이며 운영 데이터·자격증명은 저장소 밖의 로컬 `data_root` 및 자격증명 경로에 둔다. HF `pacappkor/kapa-market-data`는 public, `pacappkor/kapa-market-data-staging`은 private 검증용이다(2026-11-11 정책). knowledge-index, evals, property, matters는 private을 유지하고 raw·로그는 업로드하지 않는다. 실제 가시성과 설정이 다르면 어느 방향으로도 자동 변경하지 않고 거부한다.

설정 예시 (`../_kapa_data/runtime.config.json`, 경로는 이 파일 기준)

```json
{
  "workspace": "..",
  "data_root": ".",
  "credential_files": {
    "data_go_kr": "../data_gov_key.txt",
    "hugging_face_write": "../hf_write_token.txt",
    "hugging_face_read": "../hf_read_token.txt"
  },
  "hf_namespace": "example-org",
  "hf_repos": {"market": {"name": "kapa-market", "visibility": "private"}}
}
```

market을 공개하려면 위 visibility만 `public`으로 명시한다. 문자열 형식 `"market": "kapa-market"`은 기존 호환 private 설정이다. 다른 역할의 public 설정은 오류다. public 업로드 직전에도 모든 서비스의 `terms_reviewed=true`와 `terms_basis`를 다시 확인한다. 확인된 시장 자료 이용조건은 `이용허락범위 제한 없음`으로 기록하되 출처·검토일도 남긴다.

`examples/operator-policy.example.json`을 data_root의 `operator-policy.json`으로 복사한다. 실제 서비스 승인·잔여 호출 예산을 확인하여 서비스별 `max_requests`를 설정한다(3~150, 재시도를 포함해 페이지당 3회 예약, 최대 페이지 수는 floor(max_requests/3)). 일일 한도는 data.go.kr API별 10,000회, VWorld 40,000회라는 사용자 보고(`user-reported-2026-11-10`)이며 제공기관 검증은 아니다. VWorld 수집은 미구현이다. P0 호출은 Asia/Seoul 달력일·서비스별로 전체 max_requests를 SQLite에 보수적으로 예약하고 초과하면 거부한다. 각 실제 페이지·재시도 요청 직전 날짜를 다시 확인하고 자정을 넘으면 새 날짜에도 전체 run 예산을 보수적으로 예약한다. 새 날짜 잔액 부족 시 요청 전에 중단한다. 실패·미사용분도 환급하지 않으며 다른 프로그램이 소비한 한도는 별도 확인한다. `quota=null`을 확인 완료로 간주하지 않는다. 외부 저장·재배포 이용조건을 검토한 뒤에만 `terms_reviewed=true`로 바꾸고 `terms_basis`에 이용조건, 출처 URL, reviewed_at을 기록한다. 이는 법적 검증을 의미하지 않으며 manifest의 legal_verified는 항상 false다. 테스트 성공 후 `fixture_evidence.github_revision`에 해당 깨끗한 전체 HEAD SHA, `passed=true`를 기록한다. 근거가 없으면 build는 중단된다.

## 수집 계획(3a)

`planning.plan_day()`는 I/O·DB 없는 계획 함수이며 수집/예약/CLI 실행 기능은 아니다. 입력은 LAWD 목록, Asia/Seoul 오늘 날짜(aware datetime도 허용), 완전 sale+rent 관측 pair → observed_at 날짜 매핑이다. 정상 zero도 완전 관측이다. 서비스 범위의 교집합만 계획한다. sale 200601 / rent 201101은 잠정 기본값이며 실제 제공 범위 검증 전 확정하지 않는다.

서비스별 9,000 requests, pair당 서비스별 150 requests(페이지당 3회), 공동 이론 상한 60 pairs와 batch cap 60을 함께 적용한다. refresh 20%(12 pairs), audit 5%(3 pairs)를 먼저 예약하고 잔여를 backfill에 쓴다(기본 75%, 미사용 share·정수 잔여 이관). 작은 batch cap은 share를 각각 제한하며 refresh가 먼저 사용한다. `refresh_sla_shortfall`은 due backlog 중 refresh share로 처리하지 못한 수다. 이미 쓴/예약한 예산은 호출자가 서비스별 입력에서 차감해야 한다. 이 함수 자체는 원자적 quota 예약·자정 전환·byte cap을 보장하지 않는다.

순서는 refresh → audit → backfill이다. 현재월은 매일, 직전 1~3개월은 관측 후 7일, 직전 4~12개월은 관측일의 다음 달 같은 날(월말 clamp)에 due다. due는 오늘 기준 window에서 재평가한다. refresh 안에서는 최신 월부터이며 각 월은 미관측 지역, 가장 오래된 관측일, 코드 오름차순으로 선택한다. audit은 12개월보다 오래된 관측 pair를 관측일 오름차순(동률은 최신월·코드)으로 선택하며 오늘 관측한 pair는 제외한다. 성공한 완전 관측일을 다음 호출의 coverage에 반영해야 순환이 진행된다. 실패·동일 입력 재계획은 cursor를 전진시키지 않으며 예산이 달라도 미처리 지역/pair가 우선된다. 기본 현재월 269지역 / 일 12 refresh slots = 약 22.42일, 올림 23일에 전체 1회 순환한다(매일 해당 slots의 관측 성공·coverage 반영 및 같은 현재월 유지 조건). 따라서 현재월 daily는 목표 cadence이지 전국 daily SLA 보장이 아니며, 현재월 backlog가 share를 채우는 동안 이전 월 refresh는 지연될 수 있다. backfill은 서울 전체 공통 기간을 먼저 채운 뒤 전국 나머지로 넘어가며 각 단계는 최신월·코드 오름차순이다. 각 pair는 한 번만 반환한다. coverage가 모두 있더라도 stale refresh와 audit은 생길 수 있다.

오프라인 기준표는 `registry/lawd_codes.json`, 출처·hash·count는 `lawd_codes.provenance.json`이다. code.go.kr의 active 10자리 법정동 코드 앞 5자리를 고유화하고 시도 집계 `xx000`은 제외한다. 그 외 시 집계와 세종 36110은 유지한다. 별도 RTMS 사용 검증 없는 추가 제외는 하지 않는다. 폐지·분할 코드의 과거 처리와 RTMS별 지역 의미 검증은 미해결이다. 원본 ZIP/TSV를 로컬에 확보한 뒤 `uv run --locked python -I -B tools/build_lawd_codes.py <raw-file> --output-dir <directory> --download-date 2026-11-12`로 커밋된 기준표를 재생성한다. `--download-date`는 필수 YYYY-MM-DD 실제 다운로드 날짜이며 새 원본에는 그 원본의 날짜를 명시한다. 같은 원본 bytes와 2026-11-12를 주면 커밋된 JSON 두 파일을 byte-for-byte 재생성한다. 다운로드된 raw는 커밋하지 않는다. 재생성은 네트워크를 사용하지 않는다.

## 실행

저장소 루트에서 실행한다. 모든 명령은 JSON 한 개만 stdout에 출력하며 API 원문·키·상세 예외는 출력하지 않는다.

```sh
uv run --locked --extra test python -I -B -m pytest -p no:cacheprovider
uv run --locked kapa-runtime collect --config ../_kapa_data/runtime.config.json --service molit-apartment-sale --lawd-code 11110 --deal-ymd 202510 --release-id smoke-202510
uv run --locked kapa-runtime collect --config ../_kapa_data/runtime.config.json --service molit-apartment-rent --lawd-code 11110 --deal-ymd 202510 --release-id smoke-202510
uv run --locked kapa-runtime query --config ../_kapa_data/runtime.config.json --release-id smoke-202510 --lawd-code 11110 --limit 10
uv run --locked kapa-runtime build --config ../_kapa_data/runtime.config.json --release-id smoke-202510
uv run --locked kapa-runtime publish --config ../_kapa_data/runtime.config.json --release-id smoke-202510 --dry-run
uv run --locked kapa-runtime publish --config ../_kapa_data/runtime.config.json --release-id smoke-202510
uv run --locked kapa-runtime activate --config ../_kapa_data/runtime.config.json --release-id smoke-202510
uv run --locked kapa-runtime query --config ../_kapa_data/runtime.config.json --lawd-code 11110 --limit 10
uv run --locked kapa-runtime rollback --config ../_kapa_data/runtime.config.json
```

rollback은 이전 활성 릴리스가 있을 때만 실행한다. 최초 운영에서는 마지막 줄을 생략한다. 조회는 기본적으로 취소 거래를 제외한다. `--transaction-type sale|rent`, `--contract-date-from YYYY-MM-DD`, `--contract-date-to`, `--price-krw-min/max`, `--area-m2-min/max`, `--building-name-contains`, `--include-cancelled`를 사용할 수 있다.

조회 결과의 `next_cursor`를 다음 호출의 `--cursor`로 전달하면 새 프로세스에서도 이어서 읽는다. 커서는 snapshot 해시·필터 해시·offset의 base64 JSON이며 서명이나 자격증명에 의존하지 않는다. 다른 snapshot/필터에는 거부한다. offset 변경은 이미 읽을 수 있는 같은 질의의 페이지 이동만 바꾸므로 권한을 부여하지 않는다. 이전 서명 커서는 호환되지 않으며 첫 페이지부터 다시 조회한다.

## Multi-repository activation guard

`workspace` is the parent of the sibling checkouts. Optional config fields:

```json
{
  "sibling_repos": ["kapa_edu_1", "kapa_edu_2", "kapa_skills", "kapa_knowledge", "kapa_mcp"],
  "required_repos": ["kapa_knowledge", "kapa_skills"],
  "trusted_repo_owners": []
}
```

These are the defaults. Names must be unique members of this fixed sibling list;
required repositories must be included in `sibling_repos`. An explicit empty
`required_repos` opts out of activation requirements and should be used only for
isolated deployments that do not consume those inputs.

Build records full HEAD SHAs under `github_revisions` as `pacappkor/<name>`,
alongside kapa_runtime. Every present configured sibling must be a Git checkout
with no staged, tracked or untracked changes (`workspace_repository_dirty`).
Missing optional siblings are recorded as JSON null and reported in build
`warnings` and manifest `quality.workspace_warnings` with code
`workspace_repository_unavailable`; missing required siblings refuse build with
`workspace_required_repository_unavailable`. A present non-Git/unreadable checkout
refuses with `workspace_revision_unavailable`, not an optional-missing warning.
Whenever knowledge is present, its JSON `deps.lock` must pin both
`kapa_skills.commit` and `kapa_skills.vendor.commit` to the recorded skills HEAD;
missing/invalid locks or divergent pins refuse with `workspace_dependency_mismatch`.
There is no silent substitution of the latest revision.

Activate and rollback require each currently configured required sibling to be
clean and checked out at the target manifest's exact pinned HEAD. Having that
object elsewhere in the local Git object store is not sufficient. Missing pins
(including old manifests), absent repositories, dirty trees and different HEADs
refuse with `error_code: workspace_revision_mismatch` and leave the active pointer
unchanged. The operator must check out the manifest's pinned combination, confirm
the knowledge lock pair, clean or separately preserve local changes, then retry.
Do not edit manifests or weaken required_repos to bypass a mismatch. Existing
remote publication verification still applies. These checks perform only offline
Git reads with minimal environment, no optional index writes, and no
fetch/checkout/global config changes. Sibling repositories remain read-only.

Ownership is default-deny before Git runs: the tree, Git directory and config
must belong to the running user (or BUILTIN\\Administrators under the existing
Git-equivalent administrator rule) or an explicitly listed `trusted_repo_owners`
identity. This same owner policy applies to runtime source revision checks.
Only then is an exact canonical resolved-path per-command `safe.directory` allowed;
all Git invocations and checkout comparisons use that real path. Values are
Windows SIDs or qualified `DOMAIN\\account` names (case-insensitive), or decimal
POSIX UIDs. Unknown/unreadable owners refuse with
`workspace_repository_untrusted_owner`. `kapa_mcp` is owned by a separate local
sandbox account, so its owner must be explicitly listed; inspect and approve the
owners of both the checkout and `.git` (they may be different sandbox accounts).
Do not add a wildcard or copy identities without operator review. An empty list
is intentional in the example; populate it in the operator's private config.

Before status, local/worktree config is read with includes disabled; any include
or includeIf is rejected, not followed. Executable helpers (filter, fsmonitor
unless false, hooksPath, diff textconv/command, merge driver, gpg,
alternateRefsCommand and generic `.command`/`.cmd` keys except `credential.*`)
refuse with `workspace_repository_unsafe_config`.
Working and indexed `.gitattributes` plus `.git/info/attributes` must not contain
`filter=`. Submodules are unsupported (default-deny): immediately after owner/config
gating, a bounded `git ls-files -s -z` rejects any index gitlink (mode 160000) or
indexed `.gitmodules`; an on-disk `.gitmodules` or Git metadata `modules` directory
(including the common directory of a linked worktree) also rejects with
`workspace_repository_submodules_unsupported`, before any attribute walk. No
submodule child is inspected or launched. This policy covers siblings and runtime
source alike. System and
global config/system attributes are disabled, and fsmonitor/hooks are also
disabled per command. Cleanliness uses `status --porcelain=v1
--untracked-files=all --ignore-submodules=none` only after inspection. Activation
and rollback retain the outer `workspace_revision_mismatch` error contract.

Operator-owned `credential.*` settings (including `credential.helper`) and
network/transport-only settings (`core.sshCommand`, `core.askPass`, and
`protocol.*`, `uploadpack.*`, `receivepack.*` keys that do not match the denylist
below) are not rejected merely for being present: the guard runs only local
read-only commands, never network/authentication operations or fetch.
In particular, `protocol.allow` and `protocol.<name>.allow` are rejected regardless
of value (even `never`): the exact protocol predicate is
`key.startswith("protocol.") and key.endswith(".allow")` on the lowercased key.
The complete config denylist, using lowercased keys, is:

- Keys starting with `include.`, `includeif.`, `filter.`, or `gpg.`.
- Exact keys `core.hookspath`, `core.alternaterefscommand`, `core.attributesfile`,
  `core.excludesfile`, or `extensions.partialclone`.
- `core.worktree` (no submodule exception).
- `core.bare` unless its lowercased value is `false`, `no`, `off`, or `0`.
- Keys starting with `remote.` and ending with `.promisor` unless their
  lowercased value is `false`, `no`, `off`, or `0`.
- Keys starting with `protocol.` and ending with `.allow`.
- Keys ending with `.command` or `.cmd` unless starting with `credential.`.
- Keys starting with `diff.` and ending with `.textconv`.
- Keys starting with `merge.` and ending with `.driver`.
- `core.fsmonitor` unless its lowercased value is `false`, `no`, `off`, or `0`.

These settings refuse with `workspace_repository_unsafe_config`. As additional
protection, every guard Git invocation clears
credential helpers with `-c credential.helper=` and sets `GIT_TERMINAL_PROMPT=0`
and an empty `GIT_ASKPASS`. Runtime never edits input repositories or requires
operators to remove their authenticated-work configuration. These checks are not
an OS sandbox: do not run concurrently with an untrusted writer changing
repository config, attributes or ownership during inspection.

### Ownership boundary and revision-pinning policy (review round 5)

The security boundary is the ownership gate above, including the checkout, Git
directory and its configuration. Once admitted, the sibling's contents and config
are **trusted operator inputs**. This guard pins revisions and detects accidental
drift; it is not a sandbox for a hostile repository or a malicious trusted owner.
Existing hardening remains, but additional defenses solely against malicious
trusted owners are outside this policy. Avoid concurrent repository mutation.

Index `assume-unchanged` and `skip-worktree` flags refuse as dirty, even when Git
status hides their changes. Submodules always refuse, including uninitialized or
empty gitlinks; remove submodules in a separately reviewed repository change rather
than weakening this policy. Broken/unreadable sibling links are errors, never optional absence.
Only actual Git metadata directories contribute config files to the ownership
boundary; worktree-root files named `config`/`config.worktree` are ordinary inputs.
All Git calls drain stdout and stderr against one shared 4 MiB byte cap (indexed
attribute reads may use the metadata cap), kill on overflow, and retain the
30-second timeout. Each subprocess starts a new process group/session; timeout or
overflow terminates its process tree (Windows `taskkill /T /F`, POSIX group kill).
Overflow refuses as `workspace_repository_unsafe_config`. Attribute scans also
limit aggregate content to 4 MiB across working/index/info attributes and walk
entries (files/directories) to 100,000; exceeding either refuses with the same code.
Attribute bodies are checked incrementally, not retained as an unbounded list.

Ignored files remain excluded by Git definition: a revision identifies the Git
tracked tree, not the complete local filesystem. Local `.env`, generated data and
other ignored inputs are not attested by `github_revisions`; do not infer their
reproducibility from these pins. If an operator consumes such files as release
inputs, record/verify them through a separate explicit input digest contract or
track them when appropriate (never track secrets). The knowledge `deps.lock`
comparison reads the working file: if an operator deliberately ignores that lock,
its values are checked against skills pins but its bytes are not attested by the
knowledge HEAD. External local `core.attributesFile`/`core.excludesFile` settings
are rejected because they can change honest-repository cleanliness decisions.

## 릴리스 구성과 파일 위치

P0 릴리스는 아파트 매매+전월세 **두 서비스**, 하나의 지역·월을 선언한다. 첫 collect는 collecting에 머무르고 두 번째 서비스의 완전한 수집·정규화 후 normalized가 된다. 지역·월 변경은 거부한다. 완료된 같은 서비스 재호출은 기존 출력 해시를 확인한 뒤 collecting 상태로 응답하며 재수집하지 않는다. draft 중단은 guard 재검사 후 collecting으로 재개한다. 두 서비스 출력이 기록된 collecting 또는 collected 중단은 모든 파일 해시·행 수·스키마와 페이지 근거를 확인한 뒤 normalized로 재개하고, 출력 누락/변조는 failed로 전이한다. build는 normalized만 허용하며 그 전 단계는 collect로 복구한다. 범위를 늘리거나 재관측할 때 새 release-id를 사용한다. 실패·부분 릴리스는 build 불가이며 새 ID로 다시 시작한다.

- `staging/ID/raw/SERVICE/OBSERVATION/page-NNNNN.xml`: 키 제거 XML, 로컬 보존 전용. 기본 업로드 금지.
- `staging/ID/data/asset_type=apartment/transaction_type=.../lawd_code=.../contract_year=.../contract_month=.../part-00001.parquet`: 정규화 자료.
- `staging/ID/collection.json`: 로컬 서비스 완료 장부.
- `manifests/ID.json`: Git 전체 SHA와 검증된 HF commit SHA를 고정한 운영 manifest.
- `staging/ID/manifests/release.json`: 업로드 당시 validated manifest. 자기 commit SHA를 포함할 수 없으므로 로컬 published manifest와 의도적으로 다르다.
- 업로드 허용 목록: data Parquet, README.md, manifests/release.json, quality/summary.json, 명시된 schema. raw와 collection.json은 제외.
- `snapshots/ID/`: 검증 후 복사한 조회용 로컬 snapshot. active 조회는 해시·스키마·행 수를 다시 검사한다.
- `manifests/active.json`, `previous.json`: 원자적 활성 포인터 및 한 단계 rollback.
- `state/runtime.sqlite3`: 단일 writer 실행·관측·상태 이력. active 상태는 활성화 이력을 뜻하며 현재 선택은 active.json만 결정한다.

## 검증 경계

release-id 조회는 normalized 이후의 정상 상태만 허용한다. normalized/validated/uploaded-candidate는 현재 parser로 보존된 페이지를 재실행하고 파일 집합·해시·스키마·행 수를 검사한다. published/active는 장부 digest에 결속된 manifest와 snapshot의 파일 집합·해시·행 수·고정 스키마를 검사하며 현재 parser/registry로 과거 raw를 재해석하지 않는다. 새 manifest는 parser_version과 서비스별 normalized_extra_fields 허용 목록을 감사 근거로 기록한다. collecting/failed/partial/quarantined는 `query_release_state_not_ready`, 불완전하거나 변조된 출력은 `query_release_output_unverified`로 거부한다. 응답의 `release_state`와 `verification`을 함께 확인한다.

publish는 private/public 모두 build 시 기록한 이용조건 근거 digest와 현재 정책을 비교한다. 변경 시 `terms_changed_rebuild_required`로 거부하므로 기존 ID를 수정하여 재사용하지 말고 새 ID로 수집·build한다. HF 저수준 writer는 내부 구현이며 공개 API가 아니다. 게시·복원은 pipeline 명령만 사용한다. 내부 writer의 비어 있지 않은 secret scan 집합에 주입 API의 token도 자동 추가된다. 설정 파일에 없는 환경변수 자격증명도 모두 검사하고 정의된 잘못된 값은 거부한다.

draft를 디렉터리보다 먼저 장부에 기록한다. 초기 디렉터리/메타데이터 생성 중단은 같은 범위로 재개할 수 있다. 이전 버전의 장부 없는 디렉터리는 release_id·서비스·지역·월이 정확히 일치하고 completed가 빈 메타데이터만 있으며 다른 파일/디렉터리가 없을 때만 복구한다. raw/data/출력 또는 완료 기록이 있으면 `orphan_collection_artifacts_not_adoptable`로 거부하며 장부를 재구성하지 않는다. rollback도 원격 연결이 필요하며 이전 고정 SHA의 가시성·파일 집합·해시·행 수를 재검증한 후에만 포인터를 바꾼다. 원격 검증 실패 시 현재 active를 유지한다.

## 공개 승격 및 검증 근거

public market은 항상 별도 private dataset에 먼저 업로드하고 고정 SHA를 검증한다. `hf_repos.market.staging_name`은 선택 항목이며 기본값은 `<name>-staging`이다(예: `{"name":"kapa-market","visibility":"public","staging_name":"kapa-market-staging"}`). 기존 staging dataset이 public이면 자동 변경하지 않고 거부한다. 토큰에는 두 dataset의 생성/쓰기/읽기 권한이 필요하다. private-only 설정의 게시 흐름은 한 단계로 유지한다.

private 검증을 통과한 동일 바이트만 public repo에 단일 commit으로 승격하고 다시 SHA 고정 검증한다. public 검증 실패 시 이전 active manifest의 검증된 inventory(없으면 빈 파일 집합; 검증된 .gitattributes는 보존)로 복원 commit 후 quarantine한다. 승격 전 SQLite에 복원 바이트와 intent를 보존하므로 강제 종료 후 같은 ID의 publish/activate는 먼저 복원하고 `public_promotion_restored_quarantined`로 거부한다. 복원 네트워크 실패 시 journal을 유지하므로 연결 복구 후 재시도하며 다른 릴리스를 진행하지 않는다. 복원 commit은 default branch를 복구하지만 이미 생성된 HF commit의 이력 삭제를 뜻하지 않는다.

state schema는 자동으로 v2로 이행하며 repo별 .gitattributes의 첫 비밀 검사 후 SHA-256(없으면 absence)을 고정한다. 이후 내용/존재 변경은 `gitattributes_verification_failed`로 거부한다. 운영 중 baseline을 임의 초기화하지 않는다. build는 validated manifest의 canonical digest와 파일별 hash/rows를 장부에 보존한다. manifest·파일·업로드용 candidate가 바뀌면 `validated_manifest_changed_rebuild_required`로 거부한다. 이전 코드에서 build한 릴리스에는 이 근거가 없으므로 새 ID로 build한다. 공개 릴리스 manifest의 `staging_revision`과 `hugging_face_revisions`에 private staging/public SHA를 각각 기록한다.

staging 이름은 대소문자를 무시하여 모든 역할의 repo 이름 및 다른 staging 이름과 달라야 한다. private staging은 후보 allowlist와 고정된 .gitattributes만 남기는 정확한 inventory 교체이므로 실패한 후보의 이전 partition은 다음 업로드에서 삭제된다. 모든 inventory 교체는 열거한 고정 SHA를 parent_commit으로 전달하며 동시 변경 시 거부한다.

로컬 active 복원 대상이 없는 첫 공개 승격은 현재 default branch를 고정 SHA로 열거한다. .gitattributes 이외 파일이 있으면 `public_repo_not_empty_no_restore_target`로 거부하며 자동 채택·삭제하지 않는다. 비어 있거나 attributes만 있는 경우에만 그 baseline을 journal에 기록한다. .gitattributes의 존재/부재 근거는 트리 열거 성공 후에만 저장한다.

복원 전용 publisher는 현재 이용조건 승인이 철회되어도 journal에 기록된 정확한 inventory만 복원할 수 있다. 대상 identity·실제 가시성·경로/본문 비밀 검사·parent_commit 검사는 생략하지 않는다. 새 게시에는 여전히 이용조건 승인이 필요하다. 복원 결과도 반환된 SHA의 정확한 파일 집합과 해시를 재검증한 후에만 journal을 지운다. 실패 시 journal을 보존하고 운영자가 연결/동시 writer 문제를 해결한 뒤 재시도한다. 공개 `HFPublisher`와 `replace_files` API는 제거되었다. 내부 `_HubWriter`는 public candidate 직접 업로드를 거부하고 private 검증 근거와 파일 해시가 결합된 승격 intent만 허용한다. 복원 authority는 state store가 저장된 journal의 digest·release ID·repo ID·예상 head를 확인한 후에만 발급하고 쓰기 직전 다시 확인한다. 호출자가 payload dict를 전달하여 terms 검사를 우회할 수 없다.

Parquet 업로드는 metadata까지 runtime TransactionRecord Arrow schema와 정확히 일치해야 하며 binary/nested/임의 컬럼은 거부한다. 모든 문자열(known JSON extra 포함)을 decoded 값으로 비밀 검사한다. URL percent escape는 대소문자와 percent-decoded view를 함께 검사·redact한다. market 및 staging에는 역할 설정 여부와 무관하게 kapa_knowledge/kapa_mcp/kapa_skills/kapa_runtime/kapa_edu_* 이름을 사용할 수 없다. manifest pointer 읽기는 leaf와 모든 ancestor의 link/reparse 여부를 먼저 확인한다.

## 장애와 복구

auth 실패는 failed, 일부 페이지만 유효하면 partial이다. 원격 hash/행 수 검증 실패는 quarantined이며 이전 active는 변경하지 않는다. 설정과 다른 가시성·미확인 권한·비밀 감지는 업로드를 거부한다. 업로드 직후 commit SHA를 uploaded-candidate 이력에 기록한다. 이후 검증·snapshot 생성/복사 실패는 SHA와 함께 quarantined로 전이하며 운영자가 이력을 확인하고 새 릴리스로 재시작한다. 프로세스 강제 종료로 uploaded-candidate에 남으면 자동으로 건너뛰지 않는다. 원격 commit 전체 파일 목록도 고정 SHA에서 확인하며 현재 파일·.gitattributes·같은 repo의 로컬 published/active manifest에 기록된 이전 파일 외 경로는 거부한다. 이전 manifest 경로라도 후보 SHA에 남아 있으면 그 SHA에서 다시 내려받아 이전 manifest의 해시·행 수와 비교하고 현재 자격증명 전체로 비밀 검사한다. 경로만 허용 근거로 넘길 수 없고 파일 항목(path/sha256/rows)이 필요하다. 검사 실패는 quarantined로 전이한다. SQLite lock은 살아 있는 writer가 없는지 확인하기 전 삭제하지 않는다. rollback은 이전 snapshot과 manifest를 검증한 뒤 복원하며 관측 자료를 지우지 않는다. 코드·정책·서비스 승인 문제는 운영 책임자에게 전달하고 비밀이 포함된 로그나 원문은 공유하지 않는다.

복구는 journal의 `public_commit_sha`를 예상 default head로 사용한다. 다른 writer가 head를 변경했으면 덮어쓰지 않고 journal을 유지한다. commit 응답 저장 전 강제 종료 등으로 예상 head를 확정할 수 없으면 `public_recovery_expected_head_required`로 자동 복구를 거부한다. 운영자가 원격 이력과 journal을 검토해야 하며 최신 head를 임의 채택하지 않는다. 복원 commit SHA는 재검증 전에 journal에 저장하여 검증 재시도를 지원한다. 복원 검증 완료와 public published 전이 직전에 default head를 다시 비교하며 변경 시 journal을 지우거나 published로 전이하지 않는다. 모든 역할의 repo 이름 자체도 대소문자 무시 중복을 허용하지 않는다.

private 대상도 후보 inventory + 검증된 이전 published/active 파일 + 고정 .gitattributes만 남기는 parent_commit 결합 교체를 사용한다. 이전 경로를 보존할 때는 열거한 head SHA에서 실제 바이트를 내려받아 기존 manifest 해시/행 수와 현재 비밀 검사를 통과해야 한다. quarantined 후보에만 속한 잔여 경로는 삭제한다. collection.json·operator-policy.json·manifest 파일은 공통 checked metadata reader로 leaf 및 모든 ancestor의 link/reparse를 검사한 후 읽는다. SQLite journal은 별도 JSON 파일이 아니라 검증된 state DB의 행이며 digest/ID 검사를 유지한다. percent escape의 대소문자 정규화는 haystack뿐 아니라 secret needle에도 적용하고 percent-decoded secret도 검사·redact한다.

metadata reader는 no-follow/nonblocking open(플랫폼 지원 시), regular-file fstat, 검사 경로와 열린 descriptor의 identity 비교, open/read 후 ancestor 재검사를 수행하며 16 MiB 초과 파일·FIFO·device를 거부한다. 이전 published 경로의 원격 바이트가 근거와 다르면 해당 릴리스의 로컬 snapshot 전체 파일 집합·해시·행 수를 재검증하고 해당 payload의 현재 비밀 검사 후 같은 inventory commit에서 복구한다. 검증된 로컬 사본이 없으면 현재 active 소유 경로는 `active_retained_partition_unrecoverable`로 쓰기 전에 거부한다. 비활성 릴리스 경로는 삭제하고 publish 결과 warnings의 `retained_partition_removed`에 release_id/path를 기록하며 해당 장부의 fully_served=false로 표시한다. 안전을 위해 이 표시는 원격 commit 전 보수적으로 기록하므로 commit 실패 시에도 남을 수 있다.

Published `extra` now keeps only the service registry's `normalized_extra_fields`: sale permits aptDong/buyerGbn/dealingGbn/estateAgentSggNm/landLeaseholdGbn/rgstDate/sggCd/slerGbn; rent permits roadnm/roadnmbcd/roadnmbonbun/roadnmbubun/roadnmcd/roadnmseq/roadnmsggcd/sggCd/useRRRight. Unknown fields are dropped during normalization and their occurrence count is recorded in observation warnings as `unknown_provider_fields_dropped:<count>`. Sanitized raw pages retain them locally only and are never uploaded. Known fields still pass all secret scans. Mixed-case hex matching is defense-in-depth; the primary publication control is the explicit field allowlist. New candidates use `kapa-runtime-data/2`. Trusted published v1 snapshots remain supported through frozen versioned Arrow schemas; their owning manifest determines validation and all decoded strings, including legacy extra, still pass current secret scans. Unknown data schema versions refuse publication/verification; new candidates cannot use the historic schema exception.

Historical schema registry freezes the complete v1/v2 Arrow field order, types and metadata independently of future TransactionRecord changes. Retained remote files and local repair use the owning manifest's version plus matching trusted hashes; unknown versions refuse. Public private-staging uses the same retained repair/remove handler, resolving ownership through staging_revision as well as public revisions; current active ownership without a verified repair copy still refuses.

Queries pass the trusted manifest data_schema_version to every partition reader. Frozen v1 and v2 share the same column/type projection into TransactionRecord and query output; v1 extra remains legacy JSON rather than being silently re-normalized. Every file in a snapshot must match that one version, so mixed-version snapshots refuse. Retained path ownership is deterministic across upload, repair and verification: current active owner first, otherwise the most recent verified published guard timestamp, never release-ID lexical order or any matching historical hash. Only that owner's exact hash/rows/version is retained or repaired.

Published guard evidence now records `published_manifest_sha256` (canonical digest). Historical manifests, active/previous pointers, retention ownership, repairs, activation, rollback and queries must match that immutable publication evidence before use. Untrusted nonactive manifests confer no retained paths; exact inventory removes unowned leftovers. An untrusted active manifest refuses. Legacy ledger rows without a publication digest are not automatically adopted or backfilled from mutable files; operator-reviewed migration is required before they can serve as trusted publication evidence. Release-specific queries load the trusted published manifest before local validation and partition decoding, without normalization replay. Only pre-publication queries replay using the current parser.

Pinned Hub downloads first require expanded file-size metadata at the exact commit and reject missing/invalid sizes or files above the wire-size cap before downloading. Failed/refused downloads trigger best-effort cleanup of that file's in-cache snapshot, blob and partial entries; the post-download regular-file/size checks remain. Expected owner hashes are checked before decoding downloaded Parquet, and metadata size must match the downloaded byte length. Parquet secret scans use bounded record batches with configurable `MAX_DECODED_ROWS` and `MAX_DECODED_STRING_BYTES` caps (defaults: 2,000,000 rows and 256 MiB decoded string bytes); excess refuses publication. Public recovery freezes the complete ownership/hash-verified pre-promotion branch inventory, including published-but-not-active release partitions, rather than rebuilding only the active snapshot. Collection CLI usage requires `--release-id` before dispatch.

The empty-public-repository bootstrap restriction applies only when neither an active pointer nor a digest-bound published release for that repo exists. Trusted published inventory can therefore serve as a recovery baseline before first activation. Historically `active` ledger releases can be activated again after the same local/remote verification; no duplicate activation transition is inserted. Missing required retained owner paths are repaired from verified local snapshots during upload, or refused for unrecoverable active ownership; verification rejects absent required owner paths. Before any Parquet batch decode, footer metadata is checked against total, row-group, column-chunk and conservative dictionary-chunk expansion caps; batches use 128 rows and keep post-decode row/string-byte limits.

Historical activation/rollback also checks the current default-branch visibility/head together and requires every target `data/**.parquet` partition to remain present with its exact target hash, rows and schema. A later release reusing a partition path with different bytes causes `active_target_not_served_at_head` without changing the pointer; publish the older data again as a new release to serve it again. Shared repository-level README, quality summary, release manifest and `.gitattributes` describe the latest publication and are exempt from this partition-serving check, remaining verified at their own pinned revisions. Historical activation/rollback resolves required retained owners as of the target release's recorded trusted publication time, excluding later publications and later removal events. The target manifest remains authoritative for its own files. New publication/promotion and current-head recovery checks continue requiring the full current ownership inventory.

Public promotion applies destination-public tombstones after merging staging/public ownership, so staging-only remnants cannot revive removed paths. Missing/corrupt active staging partitions first use frozen owner-hash-verified public payloads before attempting local snapshot repair or refusing. Public promotion freezes the complete verified private-staging tree at its pinned SHA: candidate files, retained partitions and baseline-bound `.gitattributes`. Candidate manifest digest/checked-file evidence remains separate from the complete inventory's hashes and owning schema versions. A second region/month therefore preserves the active release's distinct partitions on the public default branch. Every publication, private or public, checks visibility and candidate default-branch head together after remote verification and again immediately before the published ledger transition; a concurrent advance refuses publication and follows the existing quarantine/recovery rules.

## Explicit legacy publication attestation

Never runs automatically. For a pre-digest published/active release, an operator may run:

```sh
kapa-runtime attest-legacy --config runtime.config.json --release-id release-11110-202510-2
```

This is a remote-read operation, not a publication or a local-manifest adoption. It requires one full repo/commit revision already recorded in that release's uploaded-candidate/published ledger evidence, the configured target identity/visibility, explicit credentials and current secret scans. Every listed file and uploaded `manifests/release.json` is downloaded at that exact SHA and checked for hashes, rows and the manifest's known frozen schema. The uploaded candidate cannot contain its own hash or commit SHA: the command deterministically derives the final published manifest by adding only that downloaded candidate hash, ledger revision, published state and ledger staging provenance, then requires canonical equality with the local final manifest. It never edits a mismatching local manifest. Only after all checks pass does it persist the published digest with `legacy_attestation` evidence containing method `legacy-remote-attestation`, commit_sha and attested_at. Missing ledger revision, changed local/remote manifest, payload mismatch or scan failure leaves the publication untrusted. Candidate-supplied staging provenance is discarded; only a strictly shaped ledger staging revision can be added back. The normal public release allowlist also applies to every attested file, so raw envelopes cannot become retained publication ownership. All pinned Hub downloads (verification, repair, promotion and attestation) share a bounded 256 MiB byte reader: standard snapshot-to-blob cache symlinks and plain files are supported, but the resolved target must be a regular file strictly inside the configured cache directory. Local data_root metadata keeps the separate no-link 16 MiB boundary. Nonpublished validated/uploaded-candidate queries must match the ledger's validated canonical manifest digest and full file inventory before selecting a schema version or replaying raw evidence. Back up the ledger before operator maintenance; do not manually insert a digest from mutable local files.

## HF credential roles and CLI output

Configure `hugging_face_write` (`HF_TOKEN_WRITE`) for publish, promotion and
restoration writes; configure `hugging_face_read` (`HF_TOKEN_READ`) for remote
verification, downloads and read-only legacy attestation. Environment variables
win over their corresponding files; an explicitly empty or invalid role credential
fails closed, never falling back to a more privileged token. All configured aliases
(including legacy aliases) participate in secret scanning.

Legacy `hugging_face` / `HF_TOKEN` remains a write fallback and, only when no read
alias is configured, a read fallback. `diagnose` reports both roles without values
and emits `legacy_hugging_face_used_for_read` when the legacy token supplies reads.
A new write alias alone does not supply reads: configure a read token explicitly.

CLI runs disable Hub progress bars and the Windows symlink cache warning only in
the CLI process. Residual library output goes to stderr; stdout is one JSON result.

## 로컬 조회 MCP (stdio)

조회 프로세스는 운영 수집 CLI와 분리한다. `uv sync --locked --extra mcp`로 설치한다.
자격증명 없이 아래와 같은 전용 설정을 만들고 `KAPA_RUNTIME_CONFIG`로 명시한다.
상대 경로는 설정 파일 디렉터리 기준이며 data_root는 운영자가 이미 활성화한 로컬 자료를 가리킨다.

```json
{"workspace": ".", "data_root": "../_kapa_data"}
```

```sh
KAPA_RUNTIME_CONFIG=/absolute/path/query.config.json kapa-runtime-mcp
```

Windows에서는 실행할 프로세스의 환경변수에 같은 이름을 설정한다. 클라이언트 등록용
예시 (사용자가 경로를 교체하여 수동 등록; 이 구현은 실제 클라이언트 설정을 변경하지 않는다)

```json
{
  "mcpServers": {
    "kapa-runtime-data": {
      "command": "/absolute/path/venv/bin/kapa-runtime-mcp",
      "args": [],
      "env": {"KAPA_RUNTIME_CONFIG": "/absolute/path/query.config.json"}
    }
  }
}
```

Windows command는 가상환경의 `Scripts/kapa-runtime-mcp.exe`를 사용한다. Codex 등 다른
클라이언트도 동일 command/args/env의 stdio 서버로 등록한다. HTTP transport나 외부 endpoint는 없다.
stdout은 MCP 프로토콜 전용이다. 운영자가 네트워크 없는 OS 계정/샌드박스와 data_root 읽기 전용
권한을 적용할 수 있다. 서버 자체는 자격증명 모듈/HF SDK를 import하지 않고 토큰 환경변수나
credential 파일을 읽지 않는다. 다운로드·수집·상태 전이·ledger 생성/마이그레이션도 하지 않는다.

The shared manifest contract uses fully qualified GitHub `owner/repo` IDs, never
aliases such as `runtime`. The runtime builder records `pacappkor/kapa_runtime`
and the configured `pacappkor/<sibling>` repositories. Every recorded revision is
a full 40-hex commit SHA; only the five supported workspace sibling repositories
may record `null` when unavailable. Required-sibling presence and checkout/lock
compatibility are administrative activation checks, not query-time checks.
The MCP reader validates the recorded manifest, publication digest and local data;
it never runs Git, inspects sibling checkouts/`deps.lock`, or requires the query
machine's workspace configuration to reproduce the publisher's checkout layout.

도구는 정확히 두 개이며 모두 readOnlyHint=true, openWorldHint=false다.

- `list_data_capabilities(service_id=null)`: packaged registry의 서비스, 승인·operation 검증 상태,
  필수 인자·패턴·page_size_max와 사용자 보고 quota를 반환한다. 실시간 승인/잔여량 점검이 아니다.
  현재 snapshot coverage나 미구현 서비스의 수집 가능성을 보증하지 않는다.
- `search_transactions(filters={}, limit=100, cursor=null)`: active.json의 published digest와
  read-only SQLite publication evidence를 대조하고 로컬 snapshot 전체 해시·schema·행 수를
  확인한다. staging/release_id/latest 경로는 제공하지 않는다. 허용 필터는 asset_type,
  transaction_type, lawd_code, contract_date_from/to(ISO 날짜), price_krw_min/max(정수 KRW),
  area_m2_min/max(정확한 decimal 문자열 또는 정수), building_name_contains, include_cancelled(boolean).
  limit은 1..1000이다. 취소는 기본 제외하며 data.excluded에 사유/건수를 보존한다.
  next_cursor는 snapshot 및 필터 digest에 결속한다. ACTIVE 교체 후 다른 snapshot의 cursor는
  거부하므로 처음부터 다시 조회한다. 면적 Decimal은 정확도를 위해 JSON 문자열로 반환한다.

공통 envelope는 schema_version/request_id/status/data/provenance/snapshot_refs/coverage/warnings/
unresolved_gaps/next_cursor/observed_at/legal_verified=false를 포함한다. provenance에는 release_id,
canonical manifest_sha256, HF repo_id+commit_sha가 있고 coverage는 release/query를 구별한다.
observed_at은 이 응답을 만든 시각이지 공급자 공표시각이 아니다. status는 ok, no_data(검증된
조회 0건 또는 registry 필터 일치 없음), no_active_release(포인터 없음), error를 구별한다.
error의 data.error_code는 invalid_query/active_snapshot_unverified/query_failed이며 내부 예외나
경로를 반환하지 않는다. 손상·미검증 snapshot을 정상 0건으로 바꾸지 않는다. 이 MCP는 로컬
해시 일치만 확인하며 현재 HF head 상태나 법적 적용을 검증하지 않는다. 반환 행 수는 제한되지만
검증은 snapshot 전체를 읽으므로 대용량 snapshot에서는 비용을 고려한다.

검증은 `uv run --locked --extra test --extra mcp python -I -B -m pytest -p no:cacheprovider -q`로
실행한다. 공식 SDK in-memory/stdio initialize·list·call 테스트이며 실제 채팅 제품 등록/인수나
실제 HF/API 연결 성공을 의미하지 않는다.

## 누적 릴리스(설계 단계)

**아직 운영 불가(not yet operational).** [ADR-0010](adr/0010-cumulative-batch-releases.md)의
manifest v2와 shard, 순수 assembly 함수만 제공한다. 기존 CLI/build/publish/activate와
v1 pipeline은 바뀌지 않았다. 예제 root/shard를 ACTIVE에 복사하거나 기존 publish에
넣지 않는다. 이 계약 추가는 수집·게시 실행 승인이 아니다.

`examples/cumulative-root.example.json`과 그 `coverage_shards`에 명시된 JSON은
합성 fixture이며 실제 거래나 게시 근거가 아니다. `validate_manifest`는 root를,
`resolve_snapshot_inventory(manifest, loader)`는 명시된 hash-bound shard까지 검증한다.
loader는 고정 revision과 안전한 bounded metadata read를 책임진다. glob 금지,
16 MiB/metadata file, 1,024 shards, 4,096 entries/shard, 250,000 entries/snapshot
상한은 ADR의 counting 규칙을 따른다. coverage는 정확한 K 집합이지 곱집합이 아니다.
zero는 수집 완료이며 미수집 gap과 구분한다. 현재 선택만 조회하고 archive는 기본 조회에서 제외한다.

후속 rollout은 서울 우선·최신 월 우선·두 서비스 공통 기간의 완전 pair만 대상으로 한다.
서비스당 9,000 requests/day, batch 60 pairs / 변경 512 MiB 이내로 제한할 계획이다.
수동 smoke 성공 후에만 일일 자동 publish+activate를 도입하고 오류 시 중지한다.
현재 quota/CLI가 이 신규 정책을 구현했다고 해석하지 않는다. parent 재확인,
origin 검증, tombstone-after-union, write-once 경로, atomic activation 및
published-but-not-active 차단을 통합·검증하기 전에는 누적 운영을 시작하지 않는다.

## 누적 수집 job (3b, opt-in)

`plan --config runtime.json` 또는 `collect-batch --config runtime.json --job-id day-20261112 --dry-run`은
네트워크와 credential 접근 없이 다음 계획·서비스별 보수적 예약량을 표시한다. 일관된
ledger 읽기를 위해 기존 writer lock을 취하며 opt-in schema 4를 생성한다. 기본 v1 호출은
schema 2를 유지한다. 이미 schema 3/4인 DB는 downgrade하지 않는다.

실수집은 **운영자 smoke test 전 금지**다. 검증된 누적 v2 ACTIVE와 해당 로컬 snapshot,
producer 원본 manifest 및 publication ledger가 필요하다. v1 ACTIVE에는 `bootstrap_required`를
반환한다. legacy bootstrap은 후속 단계다. 실제 API 제공 시작월, city aggregate 의미,
키·승인·schema·약관을 운영자가 smoke로 확인한 뒤에만 operator-policy.json의
`accumulation_smoke_verified: true`를 설정한다. 기존 서비스 `max_requests`(3의 배수,
3~150), terms_reviewed/terms_basis, fixture_evidence가 여전히 필수다.

실행 명령은 `collect-batch --config runtime.json --job-id day-20261112`다. 한 job은 최대
60 pair의 계획을 고정하고 각 pair 시작 전에 sale/rent **둘 다** runs ledger에 원자적으로
예약한다. 각 요청 직전 Seoul 날짜를 확인하여 자정에는 두 서비스를 다시 선예약한다.
미사용 예약은 환급하지 않는다. 서비스별 9,000/day 및 더 작은 operator/provider 한도를
준수한다. HTTP 429는 이 경로에서 자동 재시도하지 않는다. v1 adapter 기본 동작은 유지한다.

`collect-batch` 종료 코드는 다음과 같다. `0`은 `validated`(로컬 build 완료), `empty`(수집 대상 없음),
또는 명시적 `--dry-run`의 `planned`만 의미한다. `3`은 `paused`이며 자동화는 같은 job-id로
재개를 예약해야 한다. `1`은 `blocked`/오류 및 그 밖의 미완료·알 수 없는 상태이고,
`2`는 설정 오류 또는 CLI 인자 오류다. 성공 코드는 게시나 ACTIVE 전환을 의미하지 않는다.
각 서비스의 `max_requests`는 생략할 수 없는 정수(3~150, 3의 배수)이며 기본값으로 대체하지 않는다.
실수집 전에 `terms_basis`가 공백 제거 후 비어 있지 않은 문자열인지 검증한다.
job-id는 ledger 기록·경로 사용 전에 설정된 모든 secret으로 검사하며 일치하면
값을 노출하지 않고 `identifier_scan_failed`로 거부한다.

재개는 반드시 같은 job-id를 사용한다. page 중단은 새 attempt/run/observation으로 page 1부터
시작하며 성공한 sibling은 page hash·schema·행수·정규화 replay·secret scan 후 재사용한다.
partial을 합치지 않는다. `paused`는 quota/provider 중단이며 다음 날 같은 job을 재개한다.
`blocked`는 auth/schema/security/verification 또는 partition/batch cap 중단이다. 다른 job-id나
날짜로 우회할 수 없으며 **운영자 조사 전 DB state를 수정하지 않는다**. 자동 unblock CLI는 없다.
프로세스 강제 종료로 남은 `.lock`은 자동 탈취하지 않는다. 운영자는 해당 writer가 종료했음을
확인하고 백업·원인 조사 후에만 lock을 제거한다. URL/키/payload/traceback은 출력하지 않는다.

완전 pair만 data_root/accumulation의 write-once observation에서 선택하여 cumulative snapshot을
만든다. 변경 Parquet 합계 512 MiB를 넘으면 게시 후보를 만들지 않고 중지한다(자동 분할 없음).
로컬 원시 XML은 보존하되 snapshot에는 포함하지 않는다. 결과 `validated`는 로컬 build 완료일
뿐 publication/ACTIVE 전환이 아니다. 결과는 snapshots/<job-id> 및 manifests/<job-id>.json에 있다.
**기존 v1 publish에 넣지 않는다.** Step 4는 job built→게시 완료 lifecycle, 게시 직전 parent 재검사와
publication ledger 전이를 통합해야 한다. 그 전에는 built job이 새 job 시작을 차단한다.
중간 materialization 디렉터리가 불완전하면 재개가 fail-closed되므로 운영자 복구가 필요하다.

runtime config `exclude_parent_city_aggregates`는 기본 true다. committed LAWD 표에서 끝자리 0인
시 이름과 같은 앞 4자리·시 이름 prefix를 가진 하위 구가 존재하면 시 집계만 제외한다
(41110 제외, 41111/41113 유지; 36110 유지). false로 전체 active 표를 쓸 수 있다.
**잠정 RTMS 정책이며 operator smoke 대기 중**이다. 원본 표·provenance는 수정하지 않고 두 파일
digest, 이 flag, policy digest, parser version 및 plan digest를 job에 결속한다.

## Cumulative ACTIVE query and coverage (step 5)

Install the optional server with `uv sync --locked --extra test --extra mcp`.
`kapa-runtime-mcp` exposes three read-only, credential-free tools:
`list_data_capabilities`, `search_transactions`, and `get_data_coverage`.
No tool collects, publishes, opens arbitrary paths, accepts SQL, or contacts a provider.

For v2 ACTIVE, the publication ledger binds one captured root digest. The reader
resolves explicit hash-bound coverage shards and current files; retained/archive
files are never globbed or queried. Every request verifies the entire current
inventory before and after execution, including unqueried partitions. Selected
files additionally receive pre/post hash/schema/decoded-row checks. Verification
also checks data against partition region/month/service/observation metadata.
This retains full verification I/O; query pruning is not a reduced trust mode.

Example search arguments: `{"filters":{"lawd_code":"11110","contract_date_from":"2025-10-01"},"limit":100}`.
Coverage arguments: `{"filters":{"service_id":"molit-apartment-sale","lawd_code":"11110"},"limit":100}`.
Coverage also accepts `contract_date_from/to`. Limits are 1..1000. Follow
`next_cursor` until null, using identical filters. Cursors bind root digest,
query and stable ordering; restart pagination after activation or provenance changes.

Coverage pages contain distinct exact service/region/month keys, with separate
`declared`, `observed`, `zero`, `not_collected` memberships. Never multiply region
and month lists. Zero means observed with zero transactions, not missing data.
`gaps_overlapping_query` is page-local for coverage pages; `complete` describes
the whole filtered declared set. Unstructured manifest gaps remain separately
reported and cannot safely be assigned to a particular partition.
For both query tools, `data` contains only `rows` and `total`; metadata appears
only in the envelope (`coverage.query`, `partition_provenance`, `excluded`,
`verification`, `limitations`, `warnings`, `next_cursor`). Consumers formerly
reading these fields under `data` must use their envelope locations.
Search coverage is a fixed-size pointer to `get_data_coverage`, not inline key sets.
Search provenance includes only partitions contributing returned rows, capped at
100 entries; `partition_provenance_summary` reports total contributors, limit and
truncation. Use coverage pages for complete provenance. Coverage pages retain
per-partition producer, observation, origin digest and `fetched_at`.
A valid v2 coverage filter matching no declared or observed key returns `no_data`;
a declared uncollected key still returns `ok`. Legacy coverage remains `ok` even
though it has no exact-key row count.
Staleness is explicitly unknown: no freshness SLA is inferred from collection cadence.
Inspect timestamps before relying on old observations. Cancellation exclusions,
snapshot digest, pinned dataset revisions, limitations and `legal_verified=false`
remain in responses. v1 retains its legacy coverage and query behavior.

Arrow scans use 4096-row batches and one-fragment/one-batch readahead. Count scans
project only predicate columns; row pages project the response schema. DuckDB
has a 256 MB working-memory limit and external access disabled. Deep OFFSET sorts
can fail within that bound rather than silently exhausting memory; narrow the query
or restart with smaller scope. No process-wide constant-memory claim is made.

Integrity failures at any point, including between initial verification and a
shard/data read, return `error` with `active_snapshot_unverified`, never
`invalid_query` or a normal empty result.
Fail-closed errors require checking the local snapshot and publication evidence;
never repair a manifest hash by hand or bypass the ledger. This read path does not
authorize a real cumulative publication/bootstrap rollout; see the operator checklist below.

## 누적 batch 게시 (step 4)

실제 HF 게시 전 operator smoke가 필수다. 이 구현의 fake-HF 검증은 실행 승인이나
운영 smoke 완료 증거가 아니다. 기존 `accumulation_smoke_verified` 정책, public market
명시 설정, reviewed terms, private staging, trusted ACTIVE parent가 필요하다.
빌드 완료 job만 게시하며 job의 snapshot digest가 없는 이전 실험 job은 재빌드/검토한다.

```sh
uv run --locked kapa-runtime publish-batch --config runtime.json --job-id batch-id --dry-run
uv run --locked kapa-runtime publish-batch --config runtime.json --job-id batch-id
uv run --locked kapa-runtime activate --config runtime.json --release-id batch-id --cumulative
# local ACTIVE rollback only; does not rewrite public history
uv run --locked kapa-runtime rollback --config runtime.json --cumulative
```

Dry-run은 수집/commit/ACTIVE 변경 없이 기존 HF 대상의 전체 inventory를 읽고 검증한다.
자격증명이 필요하며 staging/public 각각 `bytes`(변경 bytes), `ops`(Add+Delete),
`files`(전체 결과 파일 수), 그리고 `pairs`를 보고한다. 대상이 없으면 먼저 운영자가
설정/존재를 확인한다. 60 pairs / 512 MiB changed bytes / 500 operations 중 하나라도
초과하면 자동 분할하지 않고 중지한다. 검증 전용 cache/SQLite 초기화는 로컬에 남을 수 있다.

게시 순서는 private staging delta → pinned SHA 전체 검증 → public delta → pinned SHA
전체 검증 → published ledger다. unchanged archive를 다시 Add하지 않지만 전체 bytes를
계속 검증한다. README의 기본 데이터 목록은 current 선택만 포함한다. 원격 rogue path,
명시되지 않은 삭제, tombstone 부활, parent/head/visibility 변경을 허용하지 않는다.

종료 코드는 0=게시 성공/정상 dry-run, 1=검증·보안·복구·게시 실패,
2=설정/CLI 입력 오류다. `collect-batch`의 quota pause 코드 3과 달리 게시 rate limit은
1로 중지하며 `reason=hub_rate_limited`를 보고한다. commit 429는 최대 3회 시도,
Retry-After 최대 60초를 그대로 기다린다. 더 긴 지시는 일찍 재시도하지 않고 중지한다.

중단 시 같은 `--job-id`로 재실행한다. 원격 head가 journal의 전체 frozen inventory와
일치하면 잃어버린 commit 응답을 해결하고 중복 commit하지 않는다. 불일치/손상은
운영자 개입이 필요하며 새 job으로 우회하지 않는다. 검증 실패 후 public 복원은
restore-only authority와 frozen spool bytes만 사용한다. 복원 실패의 journal을 삭제하거나
head를 임의로 변경하지 않는다. spool은 state DB 옆 `publication-spool`에 있으며
복구 완료 전 GC 금지; 현재 자동 GC 자체가 없다. credential/raw/log 파일은 게시하지 않는다.

Operator smoke는 최소 A→B 게시/활성화→C 재관측/게시/활성화→B local rollback,
각 commit operation 수, 전체 inventory 검증, private staging 실제 visibility 및
기본 HF config의 archive 중복 배제를 확인한다. 공개 current rollback은 별도 절차다.

## ACTIVE v1 bootstrap rollout (step 6)

Operator procedure only; no task is registered and no smoke is claimed by tests.
Stop writers and back up the entire `_kapa_data` to an offline location: closed
SQLite ledger (including any WAL/SHM), manifests/ACTIVE/previous, snapshots,
staging collection/raw evidence, accumulation and publication-spool. Verify the
backup is readable; keep it through rollback checks. Never edit ledger digests.

From the runtime checkout, with the reviewed operator configuration:

```sh
# Only when the original publication lacks a ledger-bound digest:
uv run --locked kapa-runtime attest-legacy --config runtime.json --release-id r-11110-202510-pacappkor-2
uv run --locked kapa-runtime bootstrap-cumulative --config runtime.json --release-id bootstrap-active-v2 --dry-run
uv run --locked kapa-runtime bootstrap-cumulative --config runtime.json --release-id bootstrap-active-v2
uv run --locked kapa-runtime activate --config runtime.json --release-id bootstrap-active-v2 --cumulative
uv run --locked kapa-runtime query --config runtime.json --lawd-code 11110
```

The parent must be current trusted v1 ACTIVE. Resolve published-but-not-active
states first. `legacy_digest_required_run_attest_legacy` requires attestation.
`legacy_metadata_unverifiable_reobserve` requires verified original evidence from
backup or explicit re-observation with legacy collect/build/publish/activate under
a new ID, followed by bootstrap of that ACTIVE. Never invent missing evidence.
Original ledger rows, data bytes/paths, observation IDs and observation times stay
unchanged. Bootstrap is not a new observation or a smoke-completion claim.

Dry-run requires read credentials and existing HF targets; it reports parent and
manifest digests, partition count, staging/public changed bytes/operations and
backup instructions. No job, snapshot selection or remote commit is created;
cache and schema initialization may remain. Real publication reuses the frozen
step-4 spool and stage → verify → public promotion → verify path. Resume the same
release ID after interruption; do not bypass journals with a new ID.
Preparation first persists a `preparing` job with frozen inputs/digest and the new
release's invocation timestamp. It materializes into a unique temporary directory,
atomically renames the verified snapshot, then marks the job `built`. Same-ID
retries verify an installed snapshot or rebuild from the frozen inputs. Abandoned
`snapshots/bootstrap-preparing-*` temporary directories are never read or reused;
with all writers stopped, operators may remove these leftovers. A dry-run of a
`preparing` job refuses with `bootstrap_preparation_resume_required`; resume the
real command instead. Original partition `fetched_at` timestamps are preserved.

Optional imports use repeatable `--import-release older-release-id` on both preview
and real commands. Every origin needs a ledger digest, complete two-service
metadata, original ledger observations, raw hash/replay and verified local snapshot.
Conflicting pairs/paths refuse. HF file presence never extends coverage. Imports
are bounded to 60 pairs and the normal manifest/publication limits. Every origin,
including the parent, must currently be `published` or `active` in the ledger;
`legacy_serving_state_required` refuses quarantined, failed or other ineligible
states even when historical publication evidence remains.

### Smoke gate and bounded collection

1. After activation, check MCP `search_transactions` and `get_data_coverage`
   against v1: original timestamps/provenance, zero status, cancellation exclusions,
   exact coverage and `legal_verified=false`. Verify staging privacy, pinned SHA
   rereads, public head inventory and README current-only default files. Test local
   rollback to v1 and return with explicit activation; preserve evidence locally.
2. Only after those checks and explicit approval of bounded real collection, set
   operator-policy `accumulation_smoke_verified: true`. Record approval/time/evidence
   alongside policy. It is an execution gate, not proof the upcoming batch passed.
   Leave scheduling disabled until actual smoke completes; set false on any failure.
3. Set optional operator-policy scope (example reviewed month November 2026):

   ```json
   "accumulation_scope": {"lawd_codes":["11110"], "start":"202611", "end":"202611", "batch_cap":1}
   ```

   Scope validates codes after city-aggregate exclusion and preserves both services,
   cadence and quotas. Scope intersects, never expands, sale history from `200601`
   and rent history from `201101`, each ending at the current Seoul month. An empty
   service intersection refuses with `accumulation_scope_empty_service_range`,
   without creating work or claiming zero coverage.
   Policy is digest-bound: finish/resume jobs before changing it.
   Omission restores nationwide planning; do not remove it during initial rollout.

   ```sh
   uv run --locked kapa-runtime plan --config runtime.json
   uv run --locked kapa-runtime collect-batch --config runtime.json --job-id seoul-smoke-01 --dry-run
   uv run --locked kapa-runtime collect-batch --config runtime.json --job-id seoul-smoke-01
   uv run --locked kapa-runtime publish-batch --config runtime.json --job-id seoul-smoke-01 --dry-run
   uv run --locked kapa-runtime publish-batch --config runtime.json --job-id seoul-smoke-01
   uv run --locked kapa-runtime activate --config runtime.json --release-id seoul-smoke-01 --cumulative
   uv run --locked kapa-runtime query --config runtime.json --lawd-code 11110
   uv run --locked kapa-runtime rollback --config runtime.json --cumulative
   ```

   Stop unless plan is exactly one Seoul gu × one month. Check MCP too. Record
   delta counts, full inventories, original/new provenance and zero handling.
   Repeat the same pair under a new job ID when due to test re-observation without
   duplicate current rows; never edit frozen plans or bypass cadence.
4. Rollback above selects bootstrap locally, not public current view. `previous`
   is a one-step toggle, not a history stack. Select original v1 explicitly with
   `uv run --locked kapa-runtime activate --config runtime.json --release-id r-11110-202510-pacappkor-2`.
   Return with `activate --release-id bootstrap-active-v2 --cumulative`. Both require
   original inventory still served and verified at head. Never overwrite a live DB
   with an old backup to force rollback; public current rollback remains separate.
5. Verify city-aggregate behavior: `41110` (수원시) excluded, active 구 children kept,
   Seoul `11110` retained. The prefix/name rule is provisional, not availability
   evidence; confirm via separately approved bounded provider smoke before national
   rollout. Do not disable exclusions merely to force collection.
6. After smoke passes, change scope to `lawd_codes:["11110","11140"]`, same month,
   `batch_cap:2`; review plan and repeat collect/dry-publish/publish/activate/query
   with new ID `seoul-small-01`. Finish and approve before expanding scope.

### Windows Task Scheduler (documentation only)

Program: fully qualified `uv.exe`; Start in: runtime checkout. Arguments:
`run --locked kapa-runtime collect-batch --config <absolute-runtime.json> --job-id <persistent-job-id>`.
Configure no parallel instances. An operator wrapper must persist the pending ID,
reuse it on pause/crash and assign a new ID only after reviewed completion and
publication/activation. Do not schedule a fixed completed ID forever. Publication
and activation remain supervised. Never put credentials in arguments/task XML/logs.

Exit 0: inspect JSON (validated, empty, or dry-run planned). Exit 3: quota pause,
resume same ID next Seoul day. Exit 1: failure, stop for inspection, not retry loop.
Exit 2: configuration/CLI error. Publish rate limiting returns 1, not 3. Log
value-free JSON plus exit codes. Stop on parent/head/digest/visibility changes,
blocked jobs, auth/schema errors, missing origin/raw bytes, unreconciled journals,
unexpected pairs, row/zero anomalies, archive duplication or backup failure.
Monitor per-service reservations, disk/spool growth, commit operations, selected
coverage, oldest timestamps and refresh SLA shortfalls. Never delete spool/journals
to clear stops. No scheduled task is registered by this implementation.

## Unattended daily accumulation on GitHub Actions

See [Unattended accumulation setup and recovery](UNATTENDED.md) for the protected
`collector` environment, required secrets, private HF state repository creation,
one-time `state push --initial` migration, supervised main-only dispatch,
retirement of local scheduling, bundle limits/CAS, and explicit STOP clearing.
The ACTIVE v2 bootstrap and Smoke gate above remain mandatory; scheduling does
not grant collection approval or change reviewed scope.
