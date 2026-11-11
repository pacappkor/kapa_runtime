# kapa_runtime — agent rules and module contracts

Design source of truth: `../design/01..08` (workspace root, not in this repo). Read README, 03, 04, 05, 06 before coding.

## Hard rules

- This GitHub repository stays **private**. HF datasets default to **private**. Only the `market` role (reviewed public-data-derived normalized transactions) may be explicitly configured `visibility: "public"`; require recorded terms review for every released service. Knowledge, evals, property, matters, raw envelopes and logs remain private/local. Never auto-flip existing visibility or fall back to public; refuse any configured/actual mismatch.
- Credential files (`data_gov_key.txt`, `hf_token.txt`, `rone_key.txt`, `vworld_key.txt`, `edu_gh_token.txt`, `law_oc.txt`) are read **only** by `kapa_runtime.credentials` at runtime. Agents and tests must never open them, print env secrets, or embed key values. Tests use fake keys like `TESTKEY-not-real`.
- No real network in tests. Use `httpx.MockTransport` and fake `HfApi` doubles. Real API / HF smoke runs are done by the orchestrator only.
- No arbitrary URL fetch, generic SQL, arbitrary file read, or shell tools in any public API.
- Existing repos (`kapa_edu_*`, `kapa_skills`, `kapa_knowledge`, `kapa_mcp`) are read-only inputs. Runtime data lives outside this repo under a configured `data_root` (default `<workspace>/_kapa_data`).
- New files: `# SPDX-License-Identifier: GPL-3.0-only`, UTF-8, LF. No personal absolute paths (`C:/Users/...`) in committed files.
- Agents: edit only your assigned files, run tests with `uv run --locked --extra test python -I -B -m pytest -p no:cacheprovider`, do **not** commit, push, comment or merge — the orchestrator does that.
- Python 3.12, stdlib `decimal.Decimal` for money/area math, timezone-aware datetimes (Asia/Seoul for provider dates where needed).

## Module contracts (P0)

Shared types: `kapa_runtime.models` (Observation, TransactionRecord, ObservationStatus, ReleaseState, MatchStatus). Do not change without orchestrator coordination.

### config.py (agent A)
`RuntimeSettings` frozen dataclass: `workspace: Path`, `data_root: Path`, `credential_files: dict[str, Path]` (aliases `data_go_kr`, `hugging_face_write`, `hugging_face_read`, legacy `hugging_face`, `rone`, `vworld`), `hf_namespace: str | None`, `hf_repos: dict[str, str]`. `RuntimeSettings.load(path: Path | None = None)` reads JSON from the explicit path or `KAPA_RUNTIME_CONFIG`; relative paths resolve against the config file directory. Derived: `staging_dir`, `snapshots_dir`, `cache_dir`, `state_db`, `manifests_dir`, `active_manifest`.

### credentials/ (agent A)
- `Secret`: wraps a value; `repr`/`str` → `Secret(***)`; `.reveal()` only for the HTTP/HF call site.
- `load_secret(alias, settings, env=os.environ) -> Secret`. Env var (`DATA_GO_KR_SERVICE_KEY`, `HF_TOKEN`, `RONE_API_KEY`, `VWORLD_API_KEY`) wins if *defined*; defined-but-empty/invalid → `CredentialError`, no file fallback. Otherwise read only the exact configured file; strip UTF-8 BOM and trailing newline; exactly one non-empty line; reject labels like `KEY=...`, whitespace inside, reparse points/symlinks. Errors never contain the value.
- `diagnose(alias, settings, env) -> dict` with `alias, source (env|file|missing), status (ok|error), error_code`, never value/length/hash.
- `redact(text: str, secrets: Iterable[Secret]) -> str` replaces raw, URL-encoded (quote and quote_plus) forms with `***`.

### registry/ (agent A)
`registry/services.json` (packaged data) and `registry.py`: `ServiceRegistry.load()`, `get(service_id) -> ServiceSpec` (service_id, dataset_id, provider, priority, credential_alias, host, path, method, operation, required_params, page_size_max, approval_status, operation_verified, last_connection_check, quota, storage_and_redistribution_reviewed). Only the P0 services have host/path filled:
- `molit-apartment-sale`: `apis.data.go.kr` `/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade`
- `molit-apartment-rent`: `apis.data.go.kr` `/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent`
Both: params `LAWD_CD` (5 digits), `DEAL_YMD` (YYYYMM), `pageNo`, `numOfRows`; `approval_status: "connection-verified-2026-11-10"`, `operation_verified: true` (orchestrator bounded probe 2026-11-10: resultCode 000, one page). Other services from `../design/examples/services.example.json` stay `approval-unverified` with null host/path.

### adapters/molit_rtms.py (agent B)
`collect(spec, secret, *, lawd_code, deal_ymd, client: httpx.Client, run_id, page_size=1000, max_pages=50, max_bytes=5_000_000) -> CollectionResult` where `CollectionResult` has `observation: Observation` and `pages: list[SanitizedPage]` (page_no, sha256, sanitized bytes, parsed items as list[dict[str, str|None]]). HTTPS only, host/path from spec, `follow_redirects=False`, timeouts, size cap, `defusedxml`. serviceKey passed as a param (decoded key encoded once by httpx). Check `resultCode` (`000` OK; anything else → classified error, no infinite retry), `totalCount`, page count, duplicate/missing pages, totalCount drift between pages. Retry only transient (timeouts, 429, 5xx) with bounded backoff+jitter honoring Retry-After; inject sleep for tests. Status per `ObservationStatus`; zero only from a valid envelope with `totalCount=0`. Nothing containing the key (URL, exception text, headers) is stored or raised — run `credentials.redact`.

### normalization/rtms.py (agent B)
`normalize(result: CollectionResult, *, asset_type="apartment", transaction_type) -> list[TransactionRecord]`. Field map from probe (see tests/fixtures README): sale `dealAmount` (만원, "120,000") → `price_krw = int * 10000`; rent `deposit`/`monthlyRent` (만원) → separate KRW fields, never combined; `preDeposit`/`preMonthlyRent` → previous_*; `contractType` → new_or_renewal; `contractTerm`; `excluUseAr` → Decimal; `floor`, `buildYear` int; `dealYear/Month/Day` → contract_date; `cdealType`/`cdealDay` (YY.MM.DD) → cancellation; blank/space strings → None. `aptSeq` → provider_record_id when present. Unknown fields go to `extra`. `address_visibility="partial"`. Deterministic fingerprints. Raise on malformed numbers rather than guessing.

### storage/ (agent C)
- `parquet.py`: `write_partition(records, root, *, asset_type, transaction_type, lawd_code, contract_year, contract_month) -> list[WrittenFile]` (path, sha256, rows), layout `data/asset_type=…/transaction_type=…/lawd_code=…/contract_year=…/contract_month=…/part-00001.parquet`, explicit pyarrow schema (decimal128 for area, int64 KRW, date32, timestamp tz), atomic write (temp + replace), deterministic row order.
- `query.py`: `TransactionQuery(snapshot_root)`, `search(filters: dict, *, limit=100, cursor=None) -> dict` with allowed filter columns only (asset_type, transaction_type, lawd_code, contract_date from/to, price_krw min/max, area_m2 min/max, building_name contains), bound parameters, DuckDB in-memory, excludes cancelled by default (`include_cancelled` flag, with reason in output), returns `rows, total, next_cursor, coverage, warnings`; cursor bound to snapshot digest + query digest.
- `state.py`: SQLite (`state_db`) single-writer: tables `runs`, `observations`, `releases` (with `ReleaseState` transitions enforced by an allowed-transition map), `idempotency` (key = service+params+parser+scope). Exclusive lock file for writers; schema migrations versioned.

### releases/ (agent D)
- `manifest.py`: build/validate release manifest per `../design/examples/release-manifest.example.json` (release_id, state, created_at, source_runs, github_revisions, hugging_face_revisions [{repo_id, commit_sha}], parser/data schema version, files [{path, sha256, rows}], coverage, quality, unresolved_gaps, legal_verified=false).
- `hf.py`: `HFPublisher(api, namespace, repo_name)` where `api` is an injected `HfApi`-like object. `ensure_private_repo()` creates `repo_type="dataset", private=True` if absent; if it exists and is not private → refuse (no auto-fix). `upload_candidate(staging_dir, allowlist: list[str]) -> commit_sha` uploads only allowlisted relative paths (reject traversal, symlinks, files outside staging, anything matching key/token patterns or containing configured secret values) in one `create_commit`; `verify(commit_sha, manifest)` re-downloads at that exact revision into a cache and compares sha256/rows.
- `activate.py`: atomically replace `active_manifest` only after verification; keep previous for rollback; `rollback()` restores previous reference without deleting observations.

### cli.py (orchestrator integrates; agent A stubs)
`kapa-runtime diagnose` (credential diagnostics, no values), later `collect`, `build`, `publish`, `activate`.

## Test selection

Fast local checks (default selection for iterative work):
`uv run --locked --extra test python -I -B -m pytest -p no:cacheprovider -m "not slow" -q`

Full suite (required before handoff; CI continues to run all tests):
`uv run --locked --extra test python -I -B -m pytest -p no:cacheprovider -q`

The `slow` marker covers pipeline, public promotion, legacy attestation and review
integration modules. No global marker filter is configured; plain pytest runs all.
