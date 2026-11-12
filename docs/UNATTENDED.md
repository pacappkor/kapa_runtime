<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Unattended accumulation

The workflow runs main at 02:40 Asia/Seoul (17:40 UTC), after the complete offline
suite passes on the identical GitHub SHA. Existing required CI names are unchanged.
Complete the RUNBOOK ACTIVE v2 bootstrap and supervised smoke gate first. No
workflow grants terms approval, expands scope or sets the smoke flag for you.

## One-time operator setup

1. Stop and disable local scheduling. Finish pending local work on its original
   policy/source revision. Keep a verified offline backup. Confirm ACTIVE v2,
   reviewed service terms, `accumulation_smoke_verified: true`, and approved bounded
   `accumulation_scope`. Never migrate a live writer or unfinished pre-migration job.
2. Create the **private HF dataset** `pacappkor/kapa-runtime-state`. Initialize it
   with a README so the default branch has a commit. State contains private raw
   evidence and journals; never make it public. Runtime refuses configured or actual
   public visibility and never auto-flips it.
3. Give a scoped HF token read/write access to state, private
   `kapa-market-data-staging`, and public `kapa-market-data`. In the local runtime
   config retain `hf_namespace: "pacappkor"` and add to `hf_repos`:
   `"state": {"name":"kapa-runtime-state","visibility":"private"}`.
   Use the existing credential configuration. With all local writers stopped:

   ```sh
   uv run --locked kapa-runtime state push --initial --config runtime.json
   ```

   Initial push refuses an existing state index. Snapshots are excluded; keep the
   offline backup until supervised restore succeeds. External PowerShell-wrapper
   pending/STOP files are not discovered: finish its pending job first, and copy
   an outstanding STOP to `<data_root>/daily/STOP` before migrating.
4. Create GitHub environment **collector**, with secrets `KAPA_DATA_GO_KR_KEY` and
   `KAPA_HF_TOKEN`. Do not put values in variables, config or workflow inputs.
   Optional environment variable `KAPA_STATE_REPO` changes the bare state repo
   name (namespace remains pacappkor). Initially require environment reviewers.
   Remove approval requirements only after explicitly approving unattended runs.
5. Review `tools/collector-pins.json`. Knowledge's `deps.lock` must pin the same
   skills SHA in both fields. Update pins together through review. The guard checks
   clean sibling revisions and lock compatibility. mcp/edu are omitted; other
   deployments can configure `sibling_repos` without changing required siblings.
6. Merge reviewed code, then supervise `workflow_dispatch` **on main**. Verify
   restore, bounded plan/quotas, private staging verification, public publication,
   ACTIVE query and state push. Dispatch again to verify continued coverage.
   Only then permanently retire local scheduling and approve unattended environment
   access. Never run a local collector concurrently. Actions schedules are best-effort.

The runner config lives outside source checkouts. It uses explicit `credential_env`
aliases, exclusive with `credential_files`, through the same parsing/redaction and
upload-secret-scanning boundary. POSIX owner IDs use `uid:<number>`; current user
is implicitly trusted. Windows behavior remains unchanged. No state or secret is
uploaded to Actions artifacts/caches. Hub caches are ephemeral runner files.

## Bundle and concurrency protocol

`state-index.json` (`kapa-state/1`) maps relative continuation paths to SHA-256 and
size, and includes the canonical file-map digest. Bytes live at `blobs/<sha256>`.
SQLite uses its consistent backup API under the ledger writer lock. The bundle
retains manifests, policy, accumulation/raw evidence, staging, publication-spool
journals, and daily STOP/pending. It excludes snapshot trees, runtime configs,
credentials, caches, locks, WAL and SHM. Frozen pending snapshots retain generated
README/shard metadata alongside immutable evidence, not a snapshot backup.
Unchanged blobs are verified and omitted from commit operations. Obsolete blobs
are removed from the current tree; HF history still needs a retention policy.

Limits are 256 MiB/file, 2 GiB logical state, 100,000 files, 16 MiB index. A limit
refuses instead of pruning audit evidence. Plan reviewed external archival before
reaching these bounds; never manually delete ledger-bound evidence.

Pull requires an empty data root. An isolated temporary root verifies every index
path/hash/size, duplicate keys, traversal and links, then SQLite integrity and
ledger-bound manifest digests. Published snapshots are reconstructed at pinned HF
revisions through existing materialize/local verification. Historical producer
snapshots are also reconstructed for cumulative origin/shard validation; frozen
pending selections use raw/immutable observations and generated resume metadata.
No snapshot bytes are bundled. Failed reconstruction installs no ledger/receipt.

Pull records repo and head. Push refuses a moved head, checks immediately before
commit, and supplies HF `parent_commit` for atomic compare-and-swap. Initial push
also uses a parent-bound commit. Actions concurrency supplements this check.
A failed pull never triggers push; push is attempted after daily failure so STOP
and pending survive. A failed push or forcibly terminated runner may prevent that
persistence: inspect the run before retrying. Two HF repos and ephemeral runners
cannot provide a cross-repository transaction.

## Daily semantics and recovery

`daily-run` preserves wrapper exit codes: 0 success/empty/quota pause, 1 unexpected
failure (writes STOP), 4 STOP/lock refusal. Underlying `collect-batch` exit 3 /
paused retains the same pending ID next day. Activation requires a query whose
snapshot ID equals the job. Planning reuses existing coverage-aware refresh
(new/late), never-collected backfill and audit lanes.

After the identical-SHA test job passes, `daily-run --fixture-revision` generates
fixture evidence in the restored policy, preserving scope/terms/smoke approval.
Pending jobs keep their exact policy binding. A changed runtime SHA while pending
refuses with STOP, rather than rewriting frozen evidence: finish work on the
original tested revision under supervision before deploying code changes.

To clear STOP, disable scheduling/protect the environment, investigate ledger and
public/staging journal outcomes, then on a supervised machine use an empty data
root and matching config with scoped credentials:

```sh
uv run --locked kapa-runtime state pull --config recovery.json
# Resolve the cause without changing the pending ID or frozen digests.
uv run --locked kapa-runtime clear-stop --config recovery.json
uv run --locked kapa-runtime state push --config recovery.json
```

Clearing STOP does not clear blocked ledger jobs, stale locks, pending IDs or
journal conflicts. Resolve them through reviewed recovery, never digest edits or
evidence deletion. Prove no writer remains before removing a local crash lock.
Resume the same job. `clear-stop` does not modify the cloud scheduler.
