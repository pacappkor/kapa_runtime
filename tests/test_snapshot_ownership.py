# SPDX-License-Identifier: GPL-3.0-only
import copy
import json
import random

import pytest

from kapa_runtime.releases import snapshot as s
from kapa_runtime.releases.manifest_v2 import coverage_for_partitions, key
from test_manifest_v2 import partitions


def layout(seed):
    rng = random.Random(seed)
    blocks, selected, declared = {}, [], []
    for i in range(rng.randint(2, 12)):
        parts = partitions(zero=True)
        for part in parts:
            part['lawd_code'] = f'{11110 + i:05d}'
            for file in part['files']:
                file['path'] = file['path'].replace('lawd_code=11110', f'lawd_code={11110 + i:05d}')
        ds = [{k: p[k] for k in s.KEY} for p in parts]
        # The historical shard may observe both services without declaring either.
        block = dict(selected_partitions=parts, coverage=coverage_for_partitions(
            parts, ds if rng.choice([False, True]) else []))
        blocks[f'block-{i}'] = json.dumps(block).encode()
        selected.extend(parts)
        declared.extend(ds)
    parent = {'coverage_shards': [{'path': name} for name in blocks]}
    result = dict(selected_partitions=selected, coverage=coverage_for_partitions(selected, declared),
                  batch_scope={'declared_pairs': [dict(lawd_code='11110', deal_ymd='202510'),
                                                  dict(lawd_code='99999', deal_ymd='202510')]})
    result['coverage']['declared'].extend([
        dict(lawd_code='99999', deal_ymd='202510', service_id=service) for service in s.v2.SERVICES])
    return parent, result, blocks


@pytest.mark.parametrize('seed', range(50))
def test_reuse_and_new_blocks_have_unique_pair_ownership(seed):
    parent, result, blocks = layout(seed)
    expected = s._owned_pairs(result)
    # Exercise preflight as well as the final assembly path.
    s.preflight_selection(parent, result, result['batch_scope']['declared_pairs'], blocks.__getitem__)
    payloads = {}
    s._sharded_selection(result, parent, blocks.__getitem__, payloads)
    loader = lambda name: payloads[name] if name in payloads else blocks[name]
    s.check_pair_ownership(result, loader)
    owners = [s._owned_pairs(json.loads(loader(ref['path']))) for ref in result['coverage_shards']]
    assert set().union(*owners) == expected
    assert sum(map(len, owners)) == len(expected)


def test_undeclared_observed_pair_is_not_synthesized_twice():
    parent, result, blocks = layout(0)
    for name, payload in list(blocks.items()):
        block = json.loads(payload)
        block['coverage'] = coverage_for_partitions(block['selected_partitions'], [])
        blocks[name] = json.dumps(block).encode()
    payloads = {}
    s._sharded_selection(result, parent, blocks.__getitem__, payloads)
    assert len(result['coverage_shards']) == len(blocks) + 1
    assert sum(ref['path'] in blocks for ref in result['coverage_shards']) == len(blocks) - 1


def test_ownership_check_rejects_duplicate_blocks():
    parent, result, blocks = layout(0)
    parent['coverage_shards'].append(copy.deepcopy(parent['coverage_shards'][0]))
    with pytest.raises(ValueError, match='unique block'):
        s.preflight_selection(parent, result, [], blocks.__getitem__)
