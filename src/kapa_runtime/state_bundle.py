# SPDX-License-Identifier: GPL-3.0-only
"""Private, bounded content-addressed continuation state; no snapshot backups."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

from . import pipeline as p
from .credentials import load_secret
from .releases.activate import _atomic_write
from .releases.hf import _HubWriter, download_bytes, scan_secret_payload
from .releases.manifest import relative_path
from .releases.snapshot import materialize
from .storage.parquet import _safe_path
from .storage.state import StateStore

MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_FILES = 100_000
MAX_INDEX = 16 * 1024 * 1024
ROOTS = {'state', 'accumulation', 'staging', 'manifests', 'daily'}
INDEX = 'state-index.json'
RECEIPT = '.state-parent.json'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def allowed(name):
    relative_path(name)
    if (name != 'operator-policy.json' and name.split('/')[0] not in ROOTS
            or name == 'daily/lock' or name.endswith(('.lock', '-wal', '-shm'))):
        raise ValueError('state_path_refused')
    return name


@contextmanager
def exclusive(settings):
    root = settings.data_root
    _safe_path(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.with_name(root.name + '.transfer.lock')
    _safe_path(lock)
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink()


def _writer(settings, api):
    if settings.hf_visibilities.get('state', 'private') != 'private':
        raise ValueError('state_must_be_private')
    token = load_secret('hugging_face_write', settings).reveal()
    return _HubWriter(api or HfApi(token=token), settings.hf_namespace,
                      settings.hf_repos['state'], role='state', write_token=token,
                      read_token=token, secret_values=lambda: p._loaded_secrets(settings, hf=True))


def _read(writer, settings, sha, name):
    return download_bytes(writer.api, writer.repo_id, name, revision=sha,
                          cache_dir=settings.cache_dir / 'state-transfer', token=writer._read_token)


def validate_index(payload):
    if len(payload) > MAX_INDEX:
        raise ValueError('state_index_oversize')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_state_key')
            result[key] = value
        return result
    index = json.loads(payload, object_pairs_hook=unique)
    if set(index) != {'format', 'files', 'digest'} or index['format'] != 'kapa-state/1':
        raise ValueError('state_index_invalid')
    files = index['files']
    if not isinstance(files, dict) or len(files) > MAX_FILES or digest(encoded(files)) != index['digest']:
        raise ValueError('state_index_invalid')
    total = 0
    folded = set()
    for name, entry in files.items():
        allowed(name)
        if name.casefold() in folded:
            raise ValueError('state_path_collision')
        folded.add(name.casefold())
        if (not isinstance(entry, dict) or set(entry) != {'sha256', 'size'}
                or not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
                or type(entry['size']) is not int or not 0 <= entry['size'] <= MAX_FILE):
            raise ValueError('state_file_invalid')
        total += entry['size']
    if total > MAX_TOTAL:
        raise ValueError('state_bundle_oversize')
    if 'state/runtime.sqlite3' not in files or 'operator-policy.json' not in files:
        raise ValueError('state_required_file_missing')
    return index


def _reconstruct(settings, api):
    """Verify ledger anchors; reconstruct published and frozen pending selections."""
    with StateStore(settings.state_db) as store:
        store.migrate_jobs()
        manifests = list(settings.manifests_dir.glob('*.json'))
        for path in manifests:
            manifest = p._read_manifest(path)
            rid = manifest['release_id']
            target = settings.snapshots_dir / rid
            if target.exists():
                continue
            row = store.get_release(rid)
            if row and row['state'] in ('published', 'active'):
                manifest = p._trusted_published_manifest(settings, store, rid, path=path)
                publisher = p._publisher(settings, api, read_only=True)
                ref = next(r for r in manifest['hugging_face_revisions'] if r['repo_id'] == publisher.repo_id)
                def load(name):
                    return download_bytes(publisher.api, publisher.repo_id, name,
                        revision=ref['commit_sha'], cache_dir=settings.cache_dir / rid,
                        token=publisher._read_token)
                # Older v1 snapshots are supported by materialize as well.
                materialize(manifest, target, load, enabled=True)
        # Frozen pending snapshots need generated metadata plus immutable observations.
        for row in store._connection.execute("SELECT job_id,data FROM collection_jobs WHERE state='built'"):
            rid, data = row[0], json.loads(row[1])
            target = settings.snapshots_dir / p._id(rid)
            if target.exists():
                continue
            manifest = p._read_manifest(settings.manifests_dir / (rid + '.json'))
            from .releases.manifest import manifest_digest
            p._require(manifest_digest(manifest) == data['snapshot_digest'], 'frozen_manifest_changed')
            def load(name):
                for base in (settings.data_root / 'accumulation' / 'frozen' / rid,
                             settings.data_root / 'accumulation', *settings.snapshots_dir.glob('*')):
                    candidate = base / relative_path(name)
                    _safe_path(candidate)
                    if candidate.is_file():
                        return candidate.read_bytes()
                raise ValueError('pending_snapshot_evidence_missing')
            materialize(manifest, target, load, enabled=True)
        if settings.active_manifest.exists():
            manifest = p._read_manifest(settings.active_manifest)
            p._trusted_published_manifest(settings, store, manifest['release_id'], path=settings.active_manifest)
            p._verify_local(settings.snapshots_dir / manifest['release_id'], manifest)


def pull(settings, *, api=None):
    with exclusive(settings):
        root = settings.data_root
        # Never overwrite a working ledger or allow a partial restore to be saved.
        if root.exists() and any(root.iterdir()):
            raise ValueError('state_pull_requires_empty_root')
        writer = _writer(settings, api)
        writer._private()
        head, tree = writer._head_tree()
        if INDEX not in tree:
            raise ValueError('state_not_initialized')
        root.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix='state-pull-', dir=root.parent))
        staged = replace(settings, data_root=temp)
        try:
            index = validate_index(_read(writer, staged, head, INDEX))
            for name, entry in index['files'].items():
                payload = _read(writer, staged, head, 'blobs/' + entry['sha256'])
                if len(payload) != entry['size'] or digest(payload) != entry['sha256']:
                    raise ValueError('state_content_corrupt')
                target = temp / name
                _safe_path(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(target, payload)
            con = sqlite3.connect(temp / 'state/runtime.sqlite3')
            try:
                if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('state_ledger_corrupt')
            finally:
                con.close()
            _reconstruct(staged, api)
            writer._require_head(head)
            _atomic_write(temp / RECEIPT, encoded({'repo': writer.repo_id, 'head': head}))
            if root.exists():
                root.rmdir()
            os.replace(temp, root)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
        return {'state': 'restored', 'commit_sha': head}


def push(settings, *, initial=False, api=None):
    with exclusive(settings):
        writer = _writer(settings, api)
        writer._private()
        head, tree = writer._head_tree()
        receipt = settings.data_root / RECEIPT
        if initial:
            if INDEX in tree or receipt.exists():
                raise ValueError('state_already_initialized')
        else:
            _safe_path(receipt)
            bound = json.loads(receipt.read_bytes())
            if bound != {'repo': writer.repo_id, 'head': head}:
                raise ValueError('state_parent_changed')
        files, payloads = {}, {}
        total = 0
        # Writer lock spans the backup and evidence scan for a coherent bundle.
        with StateStore(settings.state_db) as store:
            with tempfile.TemporaryDirectory(prefix='state-backup-', dir=settings.data_root.parent) as directory:
                backup = Path(directory) / 'ledger.sqlite3'
                destination = sqlite3.connect(backup)
                try:
                    store._connection.backup(destination)
                finally:
                    destination.close()
                candidates = [settings.data_root / 'operator-policy.json']
                for name in sorted(ROOTS):
                    base = settings.data_root / name
                    _safe_path(base)
                    if base.exists():
                        for parent, dirs, names in os.walk(base, followlinks=False):
                            for child in dirs + names:
                                _safe_path(Path(parent) / child)
                            candidates.extend(Path(parent) / name for name in names)
                for path in candidates:
                    name = path.relative_to(settings.data_root).as_posix()
                    if name == 'daily/lock' or name.endswith(('.lock', '-wal', '-shm')):
                        continue
                    allowed(name)
                    _safe_path(path)
                    source = backup if path == settings.state_db else path
                    if not source.is_file() or source.stat().st_size > MAX_FILE:
                        raise ValueError('state_file_oversize_or_special')
                    payload = source.read_bytes()
                    scan_secret_payload(name.encode(), 'state-path', writer.secret_values())
                    scan_secret_payload(payload, name, writer.secret_values())
                    sha = digest(payload)
                    files[name] = {'sha256': sha, 'size': len(payload)}
                    payloads['blobs/' + sha] = payload
                    total += len(payload)
                    if len(files) > MAX_FILES or total > MAX_TOTAL:
                        raise ValueError('state_bundle_oversize')
        index = encoded({'format': 'kapa-state/1', 'files': files, 'digest': digest(encoded(files))})
        validate_index(index)
        scan_secret_payload(index, INDEX, writer.secret_values())
        payloads[INDEX] = index
        operations = []
        for name, payload in payloads.items():
            if name in tree:
                previous = _read(writer, settings, head, name)
                if previous == payload:
                    continue
                if name.startswith('blobs/'):
                    raise ValueError('state_remote_blob_corrupt')
            operations.append(CommitOperationAdd(path_in_repo=name, path_or_fileobj=payload))
        for name in tree:
            if name.startswith('blobs/') and name not in payloads:
                operations.append(CommitOperationDelete(path_in_repo=name))
        writer._require_head(head)
        if operations:
            result = writer.api.create_commit(writer.repo_id, repo_type='dataset', operations=operations,
                parent_commit=head, token=writer._token, commit_message='Save private continuation state')
            head = result.oid
        writer._require_head(head)
        _atomic_write(receipt, encoded({'repo': writer.repo_id, 'head': head}))
        return {'state': 'saved', 'commit_sha': head, 'files': len(files)}
