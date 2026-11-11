# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import base64
import hashlib
from urllib.parse import quote
from pathlib import Path
from types import SimpleNamespace

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from huggingface_hub.errors import RepositoryNotFoundError

from kapa_runtime.releases import PublishError, build_manifest
from kapa_runtime.releases.hf import _HubWriter


class FakeSecret:
    def reveal(self):
        return "TESTKEY-not-real"


class FakeHfApi:
    token = "TESTKEY-not-real"

    def __init__(self, *, exists=True, private=True):
        self.exists, self.private = exists, private
        self.creates, self.commits, self.downloads = [], [], []
        self.files = {}

    def repo_info(self, repo_id, **kwargs):
        assert kwargs["repo_type"] == "dataset"
        if not self.exists:
            raise RepositoryNotFoundError("missing", response=httpx.Response(404, request=httpx.Request("GET", "https://example.invalid/repo")))
        return SimpleNamespace(private=self.private, sha="b" * 40)

    def create_repo(self, repo_id, **kwargs):
        assert kwargs["private"] is True and kwargs["exist_ok"] is False
        assert kwargs["repo_type"] == "dataset"
        self.creates.append((repo_id, kwargs))
        self.exists, self.private = True, True

    def create_commit(self, repo_id, *, operations, **kwargs):
        assert self.private and kwargs["repo_type"] == "dataset"
        self.commits.append(operations)
        from huggingface_hub import CommitOperationDelete
        if "parent_commit" in kwargs:
            assert kwargs["parent_commit"] == "b" * 40
        for op in operations:
            if isinstance(op, CommitOperationDelete):
                self.files.pop(op.path_in_repo, None)
            else:
                self.files[op.path_in_repo] = op.path_or_fileobj
        return SimpleNamespace(oid="b" * 40)

    def list_repo_files(self, repo_id, *, revision, **kwargs):
        assert revision == "b" * 40 and kwargs["repo_type"] == "dataset"
        return list(self.files)

    def get_paths_info(self, repo_id, paths, *, revision, expand, **kwargs):
        assert revision == 'b' * 40 and expand is True
        return [SimpleNamespace(path=name, size=len(self.files[name]), blob_id=hashlib.sha1(self.files[name]).hexdigest()) for name in paths]

    def hf_hub_download(self, repo_id, filename, *, revision, cache_dir, **kwargs):
        assert revision == "b" * 40 and kwargs["repo_type"] == "dataset"
        self.downloads.append((filename, revision))
        dest = Path(cache_dir) / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.files[filename])
        return str(dest)


def manifest_for(files):
    return build_manifest(release_id="r1", parser_version="1", state="published",
                          github_revisions={"owner/code": "a" * 40},
                          hugging_face_revisions=[{"repo_id": "owner/data", "commit_sha": "b" * 40}],
                          files=[{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "rows": 0}
                                 for name, data in files.items()])


@pytest.mark.parametrize("token", [None, True, "", "   "])
def test_ambient_credentials_forbidden(token):
    api = FakeHfApi()
    api.token = token
    with pytest.raises(PublishError):
        _HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()])


def test_private_create_and_allowlisted_only(tmp_path):
    api = FakeHfApi(exists=False)
    publisher = _HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()])
    publisher.ensure_private_repo()
    (tmp_path / "data.json").write_bytes(b"{}")
    (tmp_path / "unlisted.json").write_bytes(b"do not upload")
    assert publisher.upload_candidate(tmp_path, ["data.json"]) == "b" * 40
    assert len(api.creates) == len(api.commits) == 1
    assert set(api.files) == {"data.json"}


def test_existing_public_and_visibility_flip(tmp_path):
    api = FakeHfApi(private=False)
    publisher = _HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()])
    with pytest.raises(PublishError):
        publisher.ensure_private_repo()
    api.private = True
    publisher.ensure_private_repo()
    api.private = False
    (tmp_path / "data.json").write_bytes(b"{}")
    with pytest.raises(PublishError):
        publisher.upload_candidate(tmp_path, ["data.json"])
    assert not api.commits and not api.creates


@pytest.mark.parametrize("name", ["../escape", "/abs", "C:/abs", "a\\b", "a/../b", "a//b", "./a",
                                   "api_key.txt", "TOKEN.json", "secrets/data.json", ".env", "a:stream"])
def test_reject_unsafe_paths(tmp_path, name):
    api = FakeHfApi()
    with pytest.raises(PublishError):
        _HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()]).upload_candidate(tmp_path, [name])
    assert not api.commits


def test_secret_bytes_and_directories(tmp_path):
    api = FakeHfApi()
    publisher = _HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()])
    (tmp_path / "data.json").write_bytes(b"prefix TESTKEY-not-real suffix")
    with pytest.raises(PublishError, match="configured secret"):
        publisher.upload_candidate(tmp_path, ["data.json"])
    (tmp_path / "folder").mkdir()
    with pytest.raises(PublishError):
        publisher.upload_candidate(tmp_path, ["folder"])
    assert not api.commits


def test_symlink(tmp_path):
    (tmp_path / "target.json").write_bytes(b"{}")
    try:
        (tmp_path / "link.json").symlink_to(tmp_path / "target.json")
    except OSError:
        pytest.skip("OS does not grant symlink creation")
    with pytest.raises(PublishError):
        _HubWriter(FakeHfApi(), "owner", "data", role="market", secret_values=lambda: [FakeSecret()]).upload_candidate(tmp_path, ["link.json"])


@pytest.mark.parametrize('failure', ['oversize', 'interrupted', 'post_size'])
def test_download_preflight_and_failed_cache_cleanup(tmp_path, monkeypatch, failure):
    from kapa_runtime.releases import hf
    cache = tmp_path / 'cache'
    sha = 'b' * 40
    blob_id = 'c' * 40
    repo = cache / 'datasets--owner--data'
    snapshot = repo / 'snapshots' / sha / 'README.md'
    blob = repo / 'blobs' / blob_id
    partial = blob.with_name(blob_id + '.incomplete')
    for path in (snapshot, blob, partial):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'stale')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'keep')
    monkeypatch.setattr(hf, 'MAX_DOWNLOAD_BYTES', 4)
    class Api:
        downloads = 0
        def get_paths_info(self, repo_id, paths, **kwargs):
            assert kwargs['revision'] == sha and kwargs['expand'] is True
            return [SimpleNamespace(path='README.md', size=5 if failure == 'oversize' else 4, blob_id=blob_id)]
        def hf_hub_download(self, *args, **kwargs):
            self.downloads += 1
            if failure == 'interrupted':
                raise OSError('synthetic interrupted download')
            snapshot.write_bytes(b'oversized')
            return snapshot
    api = Api()
    with pytest.raises((ValueError, OSError)):
        hf.download_bytes(api, 'owner/data', 'README.md', revision=sha, cache_dir=cache, token='fake')
    assert api.downloads == (0 if failure == 'oversize' else 1)
    assert not any(path.exists() for path in (snapshot, blob, partial))
    assert outside.read_bytes() == b'keep'


@pytest.mark.parametrize('limit', ['rows', 'strings'])
def test_parquet_decoded_limits_refuse_compressible_payload(monkeypatch, limit):
    from kapa_runtime.releases import hf
    from kapa_runtime.storage.parquet import SCHEMA
    class Reader:
        schema_arrow = SCHEMA
        metadata = SimpleNamespace(num_rows=2, num_row_groups=0)
        def iter_batches(self, *, batch_size):
            assert batch_size == hf.PARQUET_BATCH_ROWS
            if limit == 'rows':
                pytest.fail('row metadata cap must refuse before decoding')
            yield pa.record_batch([pa.array(['x' * 100, 'x' * 100])], names=['compressed-string'])
    monkeypatch.setattr(hf.pq, 'ParquetFile', lambda _: Reader())
    monkeypatch.setattr(hf, 'MAX_DECODED_ROWS', 1 if limit == 'rows' else 10)
    monkeypatch.setattr(hf, 'MAX_DECODED_STRING_BYTES', 150)
    with pytest.raises(PublishError, match='decoded limits exceeded'):
        hf.scan_secret_payload(b'tiny compressed payload', 'data.parquet', [FakeSecret()])


@pytest.mark.parametrize('limit', ['total', 'row_group', 'column', 'dictionary'])
def test_parquet_expansion_metadata_refuses_before_iteration(monkeypatch, limit):
    from kapa_runtime.releases import hf
    from kapa_runtime.storage.parquet import SCHEMA
    column = SimpleNamespace(total_uncompressed_size=50, has_dictionary_page=limit == 'dictionary',
                             dictionary_page_offset=10, data_page_offset=20)
    group = SimpleNamespace(total_byte_size=50, num_columns=1, column=lambda _: column)
    metadata = SimpleNamespace(num_rows=1, num_row_groups=1, row_group=lambda _: group)
    class Reader:
        schema_arrow = SCHEMA
        def __init__(self):
            self.metadata = metadata
        def iter_batches(self, **kwargs):
            pytest.fail('oversized metadata reached Arrow decoding')
    caps = {'total': 'MAX_PARQUET_UNCOMPRESSED_BYTES', 'row_group': 'MAX_ROW_GROUP_UNCOMPRESSED_BYTES',
            'column': 'MAX_COLUMN_UNCOMPRESSED_BYTES', 'dictionary': 'MAX_DICTIONARY_PAGE_BYTES'}
    monkeypatch.setattr(hf, caps[limit], 40)
    monkeypatch.setattr(hf.pq, 'ParquetFile', lambda _: Reader())
    with pytest.raises(PublishError, match='decoded limits exceeded'):
        hf.scan_secret_payload(b'tiny compressed input', 'part.parquet', [FakeSecret()])


def test_mismatching_download_never_decodes_parquet(tmp_path, monkeypatch):
    from kapa_runtime.releases import hf
    api = FakeHfApi()
    api.files = {'part.parquet': b'hostile mismatching compressed data'}
    manifest = manifest_for({'part.parquet': b'expected'})
    monkeypatch.setattr(hf.pq, 'ParquetFile', lambda _: pytest.fail('untrusted bytes decoded'))
    monkeypatch.setattr(hf.pq, 'read_metadata', lambda _: pytest.fail('untrusted footer decoded'))
    writer = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()])
    report = writer.verify('b' * 40, manifest, tmp_path / 'verify')
    assert report.errors == ('hash_mismatch',)
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    (candidate / 'README.md').write_bytes(b'new candidate')
    repairs = []
    writer.upload_candidate(candidate, ['README.md'], exact_inventory=True,
        previous_files=manifest['files'], cache_dir=tmp_path / 'repair',
        repair_retained=lambda name, entries: repairs.append(name))
    assert repairs == ['part.parquet']


def test_verify_requires_missing_retained_owner_path(tmp_path):
    api = FakeHfApi()
    api.files = {'README.md': b'candidate'}
    manifest = manifest_for(api.files)
    writer = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()])
    report = writer.verify('b' * 40, manifest, tmp_path,
        previous_files=[{'path': 'missing.parquet', 'sha256': 'a' * 64, 'rows': 1}])
    assert not report.passed and 'retained_file_missing' in report.errors


def test_revision_pinned_hash_check(tmp_path):
    api = FakeHfApi()
    api.files = {"data.json": b"{}"}
    m = manifest_for(api.files)
    publisher = _HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()])
    assert publisher.verify("b" * 40, m, tmp_path).passed
    api.files["data.json"] = b"changed"
    report = publisher.verify("b" * 40, m, tmp_path)
    assert not report.passed and report.errors == ("hash_mismatch",)
    with pytest.raises(PublishError):
        publisher.verify("main", m, tmp_path)


def test_parquet_row_count(tmp_path):
    source = tmp_path / "part.parquet"
    from kapa_runtime.storage.parquet import SCHEMA
    row = {name: None for name in SCHEMA.names}
    pq.write_table(pa.Table.from_pylist([row, row], schema=SCHEMA), source)
    api = FakeHfApi()
    api.files = {"part.parquet": source.read_bytes()}
    m = manifest_for(api.files)
    publisher = _HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()])
    assert publisher.verify("b" * 40, m, tmp_path / "cache").errors == ("row_count_mismatch",)
    m["files"][0]["rows"] = 2
    assert publisher.verify("b" * 40, m, tmp_path / "cache").passed


def test_reparse_point_rejected_without_windows_privileges(tmp_path, monkeypatch):
    import stat
    from kapa_runtime.releases import hf
    (tmp_path / "data.json").write_bytes(b"{}")
    original = Path.lstat

    def reparse_stat(path, *args, **kwargs):
        if path.name == "data.json":
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", reparse_stat)
    api = FakeHfApi()
    with pytest.raises(PublishError, match="reparse"):
        hf._HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()]).upload_candidate(tmp_path, ["data.json"])
    assert not api.commits


def test_partial_remote_verification_cannot_activate(tmp_path):
    from kapa_runtime.releases import ActivationError, activate, dump_manifest
    api = FakeHfApi()
    expected = {"a.json": b"{}", "b.json": b"[]"}
    m = manifest_for(expected)
    api.files = {"a.json": b"{}", "b.json": b"corrupt"}
    publisher = _HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()])
    report = publisher.verify("b" * 40, m, tmp_path / "cache")
    assert report.checked_files == ("a.json",) and not report.passed
    active, candidate = tmp_path / "active.json", tmp_path / "candidate.json"
    old = manifest_for(expected)
    old["release_id"] = "previous"
    dump_manifest(old, active)
    dump_manifest(m, candidate)
    original = active.read_bytes()
    with pytest.raises(ActivationError):
        activate(candidate, active, report)
    assert active.read_bytes() == original


def test_download_failure_is_safe(tmp_path):
    api = FakeHfApi()
    m = manifest_for({"missing.json": b"{}"})
    report = _HubWriter(api, "owner", "data", role="market", secret_values=lambda: [FakeSecret()]).verify("b" * 40, m, tmp_path)
    assert not report.passed and report.errors == ("file_verification_failed",)


@pytest.mark.parametrize('status,reason', [(401, 'hub_auth'), (403, 'hub_auth'),
    (404, 'hub_not_found'), (429, 'hub_rate_limited'), (500, 'hub_server'), (503, 'hub_server'),
    (400, 'unknown')])
def test_safe_hub_status_categories(tmp_path, status, reason):
    from huggingface_hub.errors import HfHubHTTPError
    class Broken(FakeHfApi):
        def create_commit(self, *args, **kwargs):
            response = httpx.Response(status, request=httpx.Request('POST', 'https://example.invalid/'))
            raise HfHubHTTPError('TESTKEY-not-real upstream body', response=response)
    (tmp_path / 'data.json').write_bytes(b'{}')
    with pytest.raises(PublishError) as caught:
        _HubWriter(Broken(), 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()]).upload_candidate(tmp_path, ['data.json'])
    assert caught.value.reason == reason
    assert 'TESTKEY' not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize('error,reason', [
    (ValueError('Invalid metadata in README.md: TESTKEY-not-real'), 'hub_metadata_invalid'),
    (httpx.ConnectError('TESTKEY-not-real'), 'hub_network'),
    (ValueError('TESTKEY-not-real'), 'unknown')])
def test_safe_hub_exception_categories(tmp_path, error, reason):
    class Broken(FakeHfApi):
        def create_commit(self, *args, **kwargs):
            raise error
    (tmp_path / 'data.json').write_bytes(b'{}')
    with pytest.raises(PublishError) as caught:
        _HubWriter(Broken(), 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()]).upload_candidate(tmp_path, ['data.json'])
    assert caught.value.reason == reason
    assert 'TESTKEY' not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_safe_scan_category(tmp_path):
    (tmp_path / 'data.json').write_text('TESTKEY-not-real')
    with pytest.raises(PublishError) as caught:
        _HubWriter(FakeHfApi(), 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()]).upload_candidate(tmp_path, ['data.json'])
    assert caught.value.reason == 'local_scan'


@pytest.mark.parametrize('role', ['knowledge', 'evals', 'property', 'matters'])
def test_public_role_boundary(role):
    with pytest.raises(PublishError):
        _HubWriter(FakeHfApi(), 'owner', 'data', role=role, expected_visibility='public',
                    terms_evidence={'sale': {'terms_reviewed': True, 'terms_basis': 'reviewed'}})


@pytest.mark.parametrize('evidence', [None, {}, {'sale': {}},
    {'sale': {'terms_reviewed': False, 'terms_basis': 'reviewed'}},
    {'sale': {'terms_reviewed': True, 'terms_basis': '  '}}])
def test_public_terms_boundary(evidence):
    with pytest.raises(PublishError):
        _HubWriter(FakeHfApi(), 'owner', 'data', role='market', expected_visibility='public', terms_evidence=evidence)


@pytest.mark.parametrize('encode', [lambda s: s, lambda s: quote(s, safe=''),
    lambda s: s.encode().hex(), lambda s: base64.b64encode(s.encode()).decode()])
def test_secret_upload_path_rejected(tmp_path, encode):
    from urllib.parse import quote
    value = 'hf_AbCd0123456789'
    class Secret:
        def reveal(self):
            return value
    name = encode(value) + '.json'
    (tmp_path / name).write_text('{}')
    api = FakeHfApi()
    with pytest.raises(PublishError) as caught:
        _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [Secret()]).upload_candidate(tmp_path, [name])
    assert caught.value.reason == 'local_scan'
    assert not api.commits


def test_url_encoded_secret_upload_path(tmp_path):
    class Secret:
        def reveal(self):
            return 'fake+credential=value'
    name = quote(Secret().reveal(), safe='') + '.json'
    (tmp_path / name).write_text('{}')
    api = FakeHfApi()
    with pytest.raises(PublishError, match='configured secret'):
        _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [Secret()]).upload_candidate(tmp_path, [name])
    assert not api.commits


@pytest.mark.parametrize('component', ['namespace', 'repo'])
def test_repository_identifiers_scanned_at_construction(component):
    value = 'hf_AbCd123456789'
    class Secret:
        def reveal(self):
            return value
    class NoCalls(FakeHfApi):
        def repo_info(self, *a, **k):
            pytest.fail('Hub call before identifier scan')
    with pytest.raises(PublishError, match='configured secret'):
        _HubWriter(NoCalls(), value if component == 'namespace' else 'owner',
                    value if component == 'repo' else 'data', role='market', secret_values=lambda: [Secret()])


def test_remote_tree_rejects_stale_raw_and_allows_known_previous(tmp_path):
    api = FakeHfApi()
    api.files = {'data.json': b'{}', 'raw/old.xml': b'old'}
    manifest = manifest_for({'data.json': b'{}'})
    publisher = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()])
    assert 'unexpected_remote_path' in publisher.verify('b' * 40, manifest, tmp_path).errors
    del api.files['raw/old.xml']
    api.files['data/previous.json'] = b'previous'
    previous = {'path': 'data/previous.json', 'sha256': hashlib.sha256(b'previous').hexdigest(), 'rows': 0}
    assert publisher.verify('b' * 40, manifest, tmp_path, previous_files=[previous]).passed
    assert ('data/previous.json', 'b' * 40) in api.downloads


@pytest.mark.parametrize('damage', ['hash', 'rows', 'secret', 'download', 'path_only', 'none'])
def test_retained_file_content_verified_at_candidate_revision(tmp_path, damage):
    path = tmp_path / 'old.parquet'
    from kapa_runtime.storage.parquet import SCHEMA
    row = {name: None for name in SCHEMA.names}
    row['extra'] = 'TESTKEY-not-real' if damage == 'secret' else '{}'
    pq.write_table(pa.Table.from_pylist([row], schema=SCHEMA), path)
    payload = path.read_bytes()
    api = FakeHfApi()
    api.files = {'data.json': b'{}', 'data/old.parquet': payload}
    current = manifest_for({'data.json': b'{}'})
    previous = {'path': 'data/old.parquet', 'sha256': hashlib.sha256(payload).hexdigest(), 'rows': 1}
    if damage == 'hash':
        previous['sha256'] = '0' * 64
    elif damage == 'rows':
        previous['rows'] = 2
    elif damage == 'download':
        original = api.hf_hub_download
        def download(repo, name, **kwargs):
            if name == previous['path']:
                raise OSError('synthetic unavailable')
            return original(repo, name, **kwargs)
        api.hf_hub_download = download
    report = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()]).verify(
        'b' * 40, current, tmp_path / 'cache',
        previous_files=[previous['path']] if damage == 'path_only' else [previous])
    assert report.passed is (damage == 'none')
    assert report.checked_files == ('data.json',)
    if damage not in ('path_only', 'download'):
        assert ('data/old.parquet', 'b' * 40) in api.downloads


def test_hf_read_write_tokens_never_cross_roles(tmp_path):
    class RoleApi(FakeHfApi):
        token = "TESTKEY-ambient-unused"

        def __getattribute__(self, name):
            method = super().__getattribute__(name)
            if name in {"repo_info", "list_repo_files", "get_paths_info", "hf_hub_download",
                        "create_repo", "create_commit"}:
                def checked(*args, **kwargs):
                    expected = "TESTKEY-write" if name in {"create_repo", "create_commit"} else "TESTKEY-read"
                    assert kwargs["token"] == expected
                    return method(*args, **kwargs)
                return checked
            return method

    api = RoleApi(exists=False)
    publisher = _HubWriter(api, "owner", "data", role="market",
        secret_values=lambda: [FakeSecret()], read_token="TESTKEY-read", write_token="TESTKEY-write")
    publisher.ensure_private_repo()
    (tmp_path / "data.json").write_bytes(b"{}")
    sha = publisher.upload_candidate(tmp_path, ["data.json"])
    manifest = manifest_for(api.files)
    assert publisher.verify(sha, manifest, tmp_path / "cache").passed
    publisher._verify_inventory(sha, api.files, tmp_path / "inventory")
