from __future__ import annotations

import hashlib
import json
import zipfile

import pytest
from cryptography.exceptions import InvalidTag

from scripts.encrypted_application_archive import (
    MANIFEST,
    create_key,
    decrypt_file,
    encrypt_file,
    inspect_archive,
    split_ciphertext,
    verify_zip,
)


def _bundle(tmp_path):
    data = {
        "cvs/engineer.pdf": b"synthetic private CV content",
        "runtime-data/jobs.json": b'{"company":"Synthetic Employer","status":"submitted"}',
    }
    entries = [
        {"archive_path": name, "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
        for name, value in data.items()
    ]
    source = tmp_path / "data.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in data.items():
            archive.writestr(name, value)
        archive.writestr(
            MANIFEST, json.dumps({"schema_version": "jobapply-private-bundle-v1", "files": entries})
        )
    key = tmp_path / "private.key"
    create_key(key)
    encrypted = tmp_path / "data.enc"
    encrypt_file(source, encrypted, key)
    return source, encrypted, key, data


def test_encrypted_bundle_restores_exact_cv_and_job_bytes(tmp_path):
    source, encrypted, key, data = _bundle(tmp_path)
    assert b"synthetic private CV content" not in encrypted.read_bytes()
    assert encrypted.read_bytes() != source.read_bytes()
    output = tmp_path / "restored"
    result = inspect_archive(encrypted, key, output_directory=output)
    assert result == {
        "files": 2,
        "bytes": sum(map(len, data.values())),
        "authenticated": True,
        "restored": True,
    }
    for name, value in data.items():
        assert (output / name).read_bytes() == value


@pytest.mark.parametrize("position", [0, 30, -1])
def test_modified_ciphertext_never_publishes_plaintext(tmp_path, position):
    _, encrypted, key, _ = _bundle(tmp_path)
    raw = bytearray(encrypted.read_bytes())
    raw[position] ^= 1
    encrypted.write_bytes(raw)
    output = tmp_path / "plaintext.zip"
    with pytest.raises((ValueError, InvalidTag)):
        decrypt_file(encrypted, output, key)
    assert not output.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_wrong_key_does_not_create_restore_directory(tmp_path):
    _, encrypted, _, _ = _bundle(tmp_path)
    wrong = tmp_path / "wrong.key"
    create_key(wrong)
    output = tmp_path / "restored"
    with pytest.raises(InvalidTag):
        inspect_archive(encrypted, wrong, output_directory=output)
    assert not output.exists()


def test_existing_key_archive_and_destination_are_preserved(tmp_path):
    source, encrypted, key, _ = _bundle(tmp_path)
    before_key, before_archive = key.read_bytes(), encrypted.read_bytes()
    with pytest.raises(FileExistsError):
        create_key(key)
    with pytest.raises(ValueError):
        encrypt_file(source, encrypted, key)
    with pytest.raises(ValueError):
        inspect_archive(encrypted, key, output_directory=tmp_path)
    assert key.read_bytes() == before_key
    assert encrypted.read_bytes() == before_archive


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "..\\escape"])
def test_unsafe_member_paths_are_rejected(tmp_path, name):
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, b"bad")
        archive.writestr(MANIFEST, "{}")
    with pytest.raises(ValueError, match="unsafe"):
        verify_zip(path)


def test_inventory_mismatch_is_not_restored(tmp_path):
    source, _, _, _ = _bundle(tmp_path)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("unlisted.txt", b"unlisted")
    with pytest.raises(ValueError, match="membership"):
        verify_zip(source)


def test_archive_key_cannot_be_encrypted_as_the_payload(tmp_path):
    key = tmp_path / "private.key"
    create_key(key)
    with pytest.raises(ValueError):
        encrypt_file(key, tmp_path / "key.enc", key)


def test_split_archive_is_verified_and_restored(tmp_path):
    _, encrypted, key, data = _bundle(tmp_path)
    directory = tmp_path / "public"
    manifest = split_ciphertext(encrypted, directory, "sample", part_bytes=128)
    assert len(manifest["parts"]) > 1
    output = tmp_path / "restore"
    result = inspect_archive(directory / "sample.json", key, output_directory=output)
    assert result["authenticated"] and result["files"] == 2
    for name, value in data.items():
        assert (output / name).read_bytes() == value


def test_modified_part_is_rejected_before_decryption(tmp_path):
    _, encrypted, key, _ = _bundle(tmp_path)
    directory = tmp_path / "public"
    manifest = split_ciphertext(encrypted, directory, "sample", part_bytes=128)
    part = directory / manifest["parts"][0]["file"]
    part.write_bytes(b"X" * part.stat().st_size)
    with pytest.raises(ValueError, match="digest"):
        inspect_archive(directory / "sample.json", key, output_directory=tmp_path / "restore")
    assert not (tmp_path / "restore").exists()
