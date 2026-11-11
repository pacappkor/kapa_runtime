<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Security policy

## Report a vulnerability privately

Use GitHub's **Security → Advisories → Report a vulnerability** for this repository:
https://github.com/pacappkor/kapa_runtime/security/advisories/new

Do not report vulnerabilities in public issues or pull requests. Include the affected
commit, impact, and minimal reproduction steps using synthetic data and fake
credentials. Never include live tokens, credential files, local operator data,
private staging contents, or personal identifiers. If private vulnerability reporting
is unavailable, do not post the details publicly; wait until maintainers enable it.

Maintainers should acknowledge and investigate through the private advisory,
coordinate a fix and regression tests, and agree on disclosure timing there.
No response-time or remediation-time guarantee is offered. Security fixes target
the current default branch; older revisions have no promised backports.

## Security boundary

Public source code does not authorize access to operator machines or private data.
Operator data and credentials remain outside the repository. The HF market dataset
`pacappkor/kapa-market-data` is public; verification staging
`pacappkor/kapa-market-data-staging` is private. Other dataset roles remain private.
Visibility mismatches fail closed; public market publication requires explicit
configuration and source-terms review. Raw envelopes, logs, and credentials are
never publication artifacts. See [RUNBOOK](docs/RUNBOOK.md) for operational controls.

If a real credential is exposed, its operator must revoke or rotate it immediately;
deleting it from a file alone does not invalidate it or remove historical copies.
