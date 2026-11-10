# SPDX-License-Identifier: GPL-3.0-only
import json
import sqlite3

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kapa_runtime import pipeline as p
from kapa_runtime.credentials import Secret
from kapa_runtime.releases.hf import scan_secret_payload, PublishError
from test_pipeline import settings, KEY, FIXTURES
from test_public_promotion import MultiRepoApi


def mixed_hex(value):
    return ''.join(c.upper() if i % 2 else c.lower() for i, c in enumerate(value.encode().hex()))


@pytest.mark.parametrize('encode', [mixed_hex, lambda s: s.encode().hex(), lambda s: s.encode().hex().upper()])
def test_hex_matching_case_insensitive(encode):
    with pytest.raises(PublishError, match='configured secret'):
        scan_secret_payload(encode(KEY).encode(), 'data.json', [Secret(KEY)])


def test_unknown_encoded_secret_never_published(settings):
    encoded = mixed_hex(KEY)
    def response(request):
        name = 'rent.xml' if 'Rent' in request.url.path else 'sale.xml'
        payload = (FIXTURES / name).read_bytes().replace(b'<numOfRows>1</numOfRows>', b'<numOfRows>1000</numOfRows>')
        payload = payload.replace(b'</item>', b'<futureField>' + encoded.encode() + b'</futureField></item>')
        return httpx.Response(200, content=payload)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        for service in p.SERVICES:
            result = p.collect(settings, service=service, lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    assert result['state'] == 'normalized'
    raw = list((settings.staging_dir / 'r1').rglob('*.xml'))
    assert any(encoded.encode() in path.read_bytes() for path in raw)
    for path in (settings.staging_dir / 'r1').rglob('*.parquet'):
        extras = pq.read_table(pa.BufferReader(path.read_bytes()))['extra'].to_pylist()
        assert all('futureField' not in extra and encoded not in extra for extra in extras)
    with sqlite3.connect(settings.state_db) as db:
        observations = [json.loads(row[0]) for row in db.execute('SELECT data FROM observations')]
    assert all('unknown_provider_fields_dropped:1' in obs['warnings'] for obs in observations)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    assert p.publish(settings, release_id='r1', api=api)['state'] == 'published'
    assert all(not name.startswith('raw/') for name in api.repos['owner/data']['files'])


def test_allowlisted_extra_still_secret_scanned(settings):
    encoded = mixed_hex(KEY)
    def response(request):
        payload = (FIXTURES / 'sale.xml').read_bytes().replace(b'<numOfRows>1</numOfRows>', b'<numOfRows>1000</numOfRows>')
        payload = payload.replace(b'<buyerGbn>', b'<buyerGbn>' + encoded.encode())
        return httpx.Response(200, content=payload)
    with httpx.Client(transport=httpx.MockTransport(response)) as client, pytest.raises(p.PipelineError):
        p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
