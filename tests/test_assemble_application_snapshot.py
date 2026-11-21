import hashlib
import json

import pytest

from scripts.assemble_application_snapshot import assemble


def entry(name, data, storage):
    return {
        "archive_path": name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "storage": storage,
    }


def setup_snapshot(tmp_path):
    base, delta, output = (tmp_path / name for name in ("base", "delta", "out"))
    base.mkdir()
    delta.mkdir()
    (base / "old.txt").write_bytes(b"old")
    (delta / "new.txt").write_bytes(b"new")
    m = {
        "schema_version": "jobapply-private-bundle-v1",
        "kind": "verified-incremental-snapshot",
        "complete_inventory": [entry("old.txt", b"old", "base"), entry("new.txt", b"new", "delta")],
    }
    (delta / "bundle-manifest.json").write_text(json.dumps(m))
    return base, delta, output, m


def test_assemble_manifest_without_batch_count_fields(tmp_path):
    base, delta, output, _ = setup_snapshot(tmp_path)
    assert assemble(base, delta, output)["assembled_files"] == 2
    assert (output / "old.txt").read_bytes() == b"old"
    assert (output / "new.txt").read_bytes() == b"new"
    assert (output / "bundle-manifest.json").is_file()


def test_fails_before_creating_output_when_base_changed(tmp_path):
    base, delta, output, _ = setup_snapshot(tmp_path)
    (base / "old.txt").write_bytes(b"bad")
    with pytest.raises(ValueError):
        assemble(base, delta, output)
    assert not output.exists()


def test_rejects_path_traversal_before_creating_output(tmp_path):
    base, delta, output, m = setup_snapshot(tmp_path)
    m["complete_inventory"].append(entry("../escape", b"bad", "delta"))
    (delta / "bundle-manifest.json").write_text(json.dumps(m))
    with pytest.raises(ValueError):
        assemble(base, delta, output)
    assert not output.exists()


def test_rejects_duplicate_normalized_paths(tmp_path):
    base, delta, output, m = setup_snapshot(tmp_path)
    m["complete_inventory"].append(entry("OLD.txt", b"old", "base"))
    (delta / "bundle-manifest.json").write_text(json.dumps(m))
    with pytest.raises(ValueError):
        assemble(base, delta, output)
    assert not output.exists()
