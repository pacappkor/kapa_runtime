# SPDX-License-Identifier: GPL-3.0-only
"""Generate secret-free runner configuration outside all source checkouts."""
import json
import os
from pathlib import Path


def main():
    workspace = Path(os.environ['GITHUB_WORKSPACE'])
    temp = Path(os.environ['RUNNER_TEMP'])
    config = {
        'workspace': str(workspace), 'data_root': str(temp / 'kapa-state'),
        'credential_env': {'data_go_kr': 'KAPA_DATA_GO_KR_KEY', 'hugging_face': 'KAPA_HF_TOKEN'},
        'hf_namespace': 'pacappkor',
        'hf_repos': {
            'state': {'name': os.environ.get('KAPA_STATE_REPO') or 'kapa-runtime-state', 'visibility': 'private'},
            'market': {'name': 'kapa-market-data', 'visibility': 'public',
                       'staging_name': 'kapa-market-data-staging'}},
        'sibling_repos': ['kapa_knowledge', 'kapa_skills'],
        'required_repos': ['kapa_knowledge', 'kapa_skills'],
        'trusted_repo_owners': ['uid:' + str(os.geteuid())],
    }
    (temp / 'runtime.json').write_text(json.dumps(config), encoding='utf-8')


if __name__ == '__main__':
    main()
