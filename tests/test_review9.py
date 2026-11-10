# SPDX-License-Identifier: GPL-3.0-only
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kapa_runtime import pipeline as p
from kapa_runtime.config import RuntimeSettings, ConfigurationError
from kapa_runtime.credentials import Secret, redact
from kapa_runtime.releases.hf import _HubWriter, PublishError, scan_secret_payload
from kapa_runtime.releases.manifest import load_manifest
from kapa_runtime.storage.parquet import SCHEMA
from kapa_runtime.storage.state import StateStore
from test_hf_publish import FakeSecret
from test_pipeline import settings, KEY, FIXTURES
from test_public_promotion import MultiRepoApi


def writer(api, **kwargs):
    return _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()], **kwargs)


def test_old_exported_writers_removed():
    import kapa_runtime.releases as releases
    import kapa_runtime.releases.hf as hf
    assert not hasattr(releases, 'HFPublisher')
    assert not hasattr(hf, 'HFPublisher')
    assert not hasattr(_HubWriter, 'replace_files')


def test_direct_public_writes_refused(tmp_path):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=False)
    pub = writer(api, expected_visibility='public', terms_evidence={'s': {'terms_reviewed': True, 'terms_basis': 'test'}})
    (tmp_path / 'data.json').write_bytes(b'{}')
    with pytest.raises(PublishError, match='private publication'):
        pub.upload_candidate(tmp_path, ['data.json'])
    with pytest.raises(PublishError, match='promotion intent'):
        pub._replace_files({'data.json': b'{}'})
    with pytest.raises(TypeError):
        writer(api, expected_visibility='public', restore_payloads={'data.json': b'{}'})
    with pytest.raises(PublishError, match='authority'):
        writer(api, expected_visibility='public', restoration=object())
    assert not api.commits


@pytest.mark.parametrize('damage', ['digest', 'release', 'repo', 'payload', 'head', 'deleted'])
def test_restoration_capability_revalidates_journal(tmp_path, damage):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=False)
    with StateStore(tmp_path / 'state.sqlite') as store:
        store.transition_release('r1', 'draft', {})
        store.save_promotion('r1', {'repo_id': 'owner/data', 'public_commit_sha': 'a' * 40,
                                   'restore_payloads': {'safe.json': b'safe'.hex()}})
        authority = store._restoration_authority('r1', 'owner/data')
        if damage == 'deleted':
            store.finish_promotion('r1')
        else:
            pending = store.promotion('r1')
            field, value = {'digest': ('journal_sha256', '0' * 64), 'release': ('release_id', 'other'),
                'repo': ('repo_id', 'owner/other'), 'payload': ('restore_payloads', {'evil.json': b'evil'.hex()}),
                'head': ('public_commit_sha', 'b' * 40)}[damage]
            pending[field] = value
            store._connection.execute('UPDATE public_promotions SET data=? WHERE release_id=?',
                                      (json.dumps(pending), 'r1'))
        with pytest.raises(ValueError, match='journal'):
            writer(api, expected_visibility='public', restoration=authority)
        assert not api.commits


def test_journal_backed_restoration_without_terms(tmp_path):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=False)
    payloads = {'safe.json': b'safe'}
    with StateStore(tmp_path / 'state.sqlite') as store:
        store.transition_release('r1', 'draft', {})
        store.save_promotion('r1', {'repo_id': 'owner/data', 'public_commit_sha': 'a' * 40,
                                   'restore_payloads': {'safe.json': b'safe'.hex()}})
        authority = store._restoration_authority('r1', 'owner/data')
        pub = writer(api, expected_visibility='public', restoration=authority)
        with pytest.raises(PublishError):
            pub._replace_files({'evil.json': b'evil'}, preserve_attributes=False, expected_parent='a' * 40)
        sha = pub._replace_files(payloads, preserve_attributes=False, expected_parent='a' * 40)
        pub._verify_inventory(sha, payloads, tmp_path / 'cache')
        pub._require_head(sha)


@pytest.mark.parametrize('name', ['kapa_knowledge', 'KAPA_MCP', 'kapa_skills', 'kapa_runtime', 'KAPA_EDU_sales'])
@pytest.mark.parametrize('target', ['name', 'staging_name'])
def test_reserved_repo_names(tmp_path, name, target):
    config = tmp_path / 'config.json'
    entry = {'name': 'market', 'staging_name': 'candidate'}
    entry[target] = name
    config.write_text(json.dumps({'workspace': '.', 'hf_repos': {'market': entry}}))
    with pytest.raises(ConfigurationError):
        RuntimeSettings.load(config)


@pytest.mark.parametrize('kind', ['binary', 'list', 'struct', 'wrong_string', 'metadata'])
def test_parquet_strict_schema(tmp_path, kind):
    value = 'TESTKEY-not-real'
    tables = {
        'binary': pa.table({'hidden': [value.encode()] * 1000}),
        'list': pa.table({'hidden': [[value]] * 1000}),
        'struct': pa.table({'hidden': [{'nested': value}] * 1000}),
        'wrong_string': pa.table({'hidden': [value] * 1000}),
        'metadata': pa.Table.from_pylist([], schema=SCHEMA.remove_metadata()),
    }
    path = tmp_path / 'data.parquet'
    pq.write_table(tables[kind], path, compression='gzip', use_dictionary=False, write_statistics=False)
    with pytest.raises(PublishError, match='schema outside'):
        scan_secret_payload(path.read_bytes(), path.name, [FakeSecret()])


@pytest.mark.parametrize('method', ['_require_head', '_head_tree'])
def test_head_and_visibility_same_response(method):
    class Flip(MultiRepoApi):
        def repo_info(self, *a, **k):
            return SimpleNamespace(private=False, sha='a' * 40)
    pub = writer(Flip())
    with pytest.raises(PublishError, match='visibility'):
        getattr(pub, method)(*(['a' * 40] if method == '_require_head' else []))


@pytest.mark.parametrize('pointer', ['active.json', 'previous.json', 'release.json'])
@pytest.mark.parametrize('ancestor', [False, True])
def test_manifest_pointer_checked_before_read(tmp_path, monkeypatch, pointer, ancestor):
    folder = tmp_path / 'manifests'
    folder.mkdir()
    path = folder / pointer
    path.write_text('{}')
    blocked = folder if ancestor else path
    original_link = Path.is_symlink
    original_read = Path.read_text
    def linked(path):
        return path == blocked or original_link(path)
    def read(path, *a, **k):
        if path == folder / pointer:
            pytest.fail('manifest read before link check')
        return original_read(path, *a, **k)
    monkeypatch.setattr(Path, 'is_symlink', linked)
    monkeypatch.setattr(Path, 'read_text', read)
    for loader in (load_manifest, p._read_manifest):
        with pytest.raises(ValueError, match='linked'):
            loader(path)


@pytest.mark.parametrize('encoded', ['fake%2bcredential%2fvalue', '%66ake%2bcredential%2fvalue'])
def test_lowercase_and_partially_encoded_redaction(encoded):
    secret = Secret('fake+credential/value')
    assert redact('prefix ' + encoded + ' suffix', [secret]) == 'prefix *** suffix'
    with pytest.raises(PublishError, match='configured secret'):
        scan_secret_payload(encoded.encode(), 'data.json', [secret])


def test_lowercase_encoded_parquet_secret(tmp_path):
    secret = Secret('fake+credential/value')
    row = {name: None for name in SCHEMA.names}
    row['extra'] = json.dumps({'hidden': 'fake%2bcredential%2fvalue'})
    path = tmp_path / 'data.parquet'
    pq.write_table(pa.Table.from_pylist([row] * 1000, schema=SCHEMA), path,
                   compression='gzip', use_dictionary=False, write_statistics=False)
    with pytest.raises(PublishError, match='configured secret'):
        scan_secret_payload(path.read_bytes(), path.name, [secret])


def test_lowercase_response_field_into_extra(settings):
    encoded = quote(KEY, safe='').replace('%2B', '%2b').replace('%2F', '%2f')
    def response(request):
        name = 'rent.xml' if 'Rent' in request.url.path else 'sale.xml'
        payload = (FIXTURES / name).read_bytes().replace(b'<numOfRows>1</numOfRows>', b'<numOfRows>1000</numOfRows>')
        payload = payload.replace(b'</item>', b'<hidden>' + encoded.encode() + b'</hidden></item>')
        return httpx.Response(200, content=payload)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        for service in p.SERVICES:
            result = p.collect(settings, service=service, lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    assert result['state'] == 'normalized'
    paths = list((settings.staging_dir / 'r1').rglob('*.parquet'))
    assert paths
    for path in paths:
        extras = pq.read_table(pa.BufferReader(path.read_bytes()))['extra'].to_pylist()
        assert all('hidden' not in extra for extra in extras)
        assert all(encoded not in extra and KEY not in extra for extra in extras)
