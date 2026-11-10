# SPDX-License-Identifier: GPL-3.0-only
import copy
import hashlib
import json

import pytest

from kapa_runtime.releases.manifest import (
    ManifestError, build_manifest, dump_manifest, dumps_manifest, load_manifest,
    loads_manifest, validate_manifest,
)


def candidate(state="draft"):
    return build_manifest(release_id="r1", parser_version="1", state=state,
                          github_revisions={"owner/code": "a" * 40},
                          hugging_face_revisions=[{"repo_id": "owner/data", "commit_sha": "b" * 40}],
                          files=[{"path": "data.json", "sha256": hashlib.sha256(b"{}").hexdigest(), "rows": 0}])


def test_canonical_round_trip(tmp_path):
    m = candidate("published")
    path = tmp_path / "manifest.json"
    dump_manifest(m, path)
    assert load_manifest(path) == m
    assert dumps_manifest(m) == dumps_manifest(dict(reversed(list(m.items()))))
    assert m["quality"]["legal_verified"] is False


@pytest.mark.parametrize("text", ['{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}', '{"x":1,"x":2}'])
def test_strict_json(text):
    with pytest.raises(ManifestError):
        loads_manifest(text)


@pytest.mark.parametrize("revision", [None, "main", "a" * 7, "z" * 40])
@pytest.mark.parametrize("field", ["github", "hf"])
def test_published_revision_must_be_sha(revision, field):
    m = candidate("published")
    if field == "github":
        m["github_revisions"]["owner/code"] = revision
    else:
        m["hugging_face_revisions"][0]["commit_sha"] = revision
    with pytest.raises(ManifestError):
        validate_manifest(m)


@pytest.mark.parametrize("change", [
    lambda m: m["quality"].update(legal_verified=True),
    lambda m: m.update(legal_verified=True),
    lambda m: m["files"][0].update(rows=True),
    lambda m: m["files"][0].update(path="../bad"),
    lambda m: m.update(created_at="2026-11-10T12:00:00"),
    lambda m: m["coverage"].update(value=float("nan")),
])
def test_invalid_contract(change):
    m = candidate()
    change(m)
    with pytest.raises(ManifestError):
        validate_manifest(m)


def test_build_sorts_and_validate_rejects_unsorted():
    m = candidate()
    extra = copy.deepcopy(m["files"][0])
    extra["path"] = "a.json"
    m["files"].append(extra)
    with pytest.raises(ManifestError):
        validate_manifest(m)
    built = build_manifest(release_id="r", parser_version="1", files=m["files"])
    assert [f["path"] for f in built["files"]] == ["a.json", "data.json"]
    # Duplicate keys nested inside a valid manifest are still rejected.
    text = json.dumps(built).replace('"passed": false', '"passed": false, "passed": true')
    with pytest.raises(ManifestError):
        loads_manifest(text)
