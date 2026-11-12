<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Unattended accumulation implementation workflow

1. Extend explicit credentials and portable ownership without changing Windows trust behavior.
2. Implement private content-addressed state index, locked SQLite backup, revision CAS and verified reconstruction. No snapshot data in the bundle.
3. Port daily orchestration with durable pending/STOP and offline fake tests.
4. Add pinned sibling runner configuration and main-only Actions after offline tests. Restore policy first, attest the tested SHA only when no frozen job is pending.
5. Document supervised migration and operational gates; run the full offline suite, lock and whitespace checks before local logical commits.

Dependencies: state restoration precedes daily execution; state saving runs after daily failure but never after a failed pull. Single Actions concurrency supplements, not replaces, remote expected-parent CAS. Migration requires stopped local writers and completed bootstrap/smoke. No remote operations are performed during implementation.

Gates: bounded untrusted index/file verification before installing state; existing manifest/ledger digest trust; pinned HF reconstruction; fixture evidence for identical source SHA; post-activation query; persistent STOP on unexpected daily failure. Merge only after independent review and supervised deployment; no push from this worktree.
