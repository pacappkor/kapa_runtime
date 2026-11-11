<!-- SPDX-License-Identifier: GPL-3.0-only -->
# P0 운영 절차

## GitHub 공개 전 설정 확인

관리자는 GitHub private vulnerability reporting을 활성화하고 [SECURITY.md](../SECURITY.md)의 비공개 신고 경로를 확인한다. `.github/workflows/traffic.yml`은 조직 템플릿을 그대로 사용하며 기본 브랜치 `main`에 반영된 뒤 매일 실행된다. 조직 reusable workflow는 `main`을 별도 `traffic` 집계 브랜치에 동기화하고 방문·clone 집계를 기록한다. 관리자는 해당 저장소에 필요한 권한의 `TRAFFIC_TOKEN` Actions secret을 제공해야 한다. 토큰 값은 코드·문서·로그에 넣지 않는다. README 배지는 첫 집계 전에는 표시되지 않을 수 있다. 코드 변경만으로 이 GitHub 설정이나 실제 가시성이 바뀌지는 않는다.

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
