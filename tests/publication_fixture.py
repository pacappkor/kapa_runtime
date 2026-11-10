# SPDX-License-Identifier: GPL-3.0-only
"""Test-only support for constructing historical publication evidence."""
import json

from kapa_runtime.releases.manifest import manifest_digest
from kapa_runtime.storage.state import StateStore


def seal_publication(settings, manifest):
    # Historical fixtures manufacture original releases after publishing a seed.
    # Never use this helper in tamper regressions or production migration.
    with StateStore(settings.state_db) as store:
        row = store.get_release(manifest['release_id'])
        for event in row['guard_evidence']:
            if event['state'] == 'published' and event['evidence'].get('commit_reread') is True:
                event['evidence']['published_manifest_sha256'] = manifest_digest(manifest)
        store._connection.execute('UPDATE releases SET guard_evidence=? WHERE release_id=?',
                                  (json.dumps(row['guard_evidence']), manifest['release_id']))
