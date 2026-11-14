"""Encrypt private ZIP bundles and restore authenticated bundles to a new folder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"JOBAPPLY-AES256GCM-V1\x00"
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024
MANIFEST = "bundle-manifest.json"


def create_key(path: Path) -> None:
    """Create a new random key without printing it or replacing an existing key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(secrets.token_bytes(32))


def _key(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) != 32:
        raise ValueError("Expected a 32-byte archive key")
    return data


def _destination(source: Path, output: Path, key_path: Path) -> None:
    if (
        output.exists()
        or output.resolve() in {source.resolve(), key_path.resolve()}
        or source.resolve() == key_path.resolve()
    ):
        raise ValueError("Destination must be a new file separate from input and key")
    output.parent.mkdir(parents=True, exist_ok=True)


def encrypt_file(source: Path, output: Path, key_path: Path) -> dict:
    _destination(source, output, key_path)
    header = MAGIC + secrets.token_bytes(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(_key(key_path)), modes.GCM(header[-NONCE_BYTES:])).encryptor()
    encryptor.authenticate_additional_data(header)
    digest = hashlib.sha256()
    created = False
    try:
        with source.open("rb") as incoming, output.open("xb") as outgoing:
            created = True
            outgoing.write(header)
            digest.update(header)
            while chunk := incoming.read(CHUNK_BYTES):
                encrypted = encryptor.update(chunk)
                outgoing.write(encrypted)
                digest.update(encrypted)
            tail = encryptor.finalize() + encryptor.tag
            outgoing.write(tail)
            digest.update(tail)
    except Exception:
        # This file was created exclusively by this operation, never a pre-existing file.
        if created:
            output.unlink(missing_ok=True)
        raise
    return {
        "format": "jobapply-aes256gcm-v1",
        "ciphertext_sha256": digest.hexdigest(),
        "ciphertext_bytes": output.stat().st_size,
    }


def decrypt_file(source: Path, output: Path, key_path: Path) -> None:
    _destination(source, output, key_path)
    header_size = len(MAGIC) + NONCE_BYTES
    remaining = source.stat().st_size - header_size - TAG_BYTES
    if remaining < 0:
        raise ValueError("Archive is truncated")
    temporary = output.with_name(output.name + "." + secrets.token_hex(8) + ".partial")
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            header = incoming.read(header_size)
            if not header.startswith(MAGIC):
                raise ValueError("Unsupported archive format")
            incoming.seek(-TAG_BYTES, os.SEEK_END)
            tag = incoming.read(TAG_BYTES)
            incoming.seek(header_size)
            decryptor = Cipher(
                algorithms.AES(_key(key_path)), modes.GCM(header[-NONCE_BYTES:], tag)
            ).decryptor()
            decryptor.authenticate_additional_data(header)
            while remaining:
                chunk = incoming.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("Archive is truncated")
                outgoing.write(decryptor.update(chunk))
                remaining -= len(chunk)
            outgoing.write(decryptor.finalize())
        # Authentication has succeeded. Exclusive creation also guards against a race.
        with temporary.open("rb") as incoming, output.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, CHUNK_BYTES)
    finally:
        temporary.unlink(missing_ok=True)


def _members(archive: zipfile.ZipFile) -> dict:
    members = {}
    for item in archive.infolist():
        path = PurePosixPath(item.filename)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in item.filename
            or ":" in item.filename
            or not path.parts
            or item.is_dir()
            or stat.S_ISLNK(item.external_attr >> 16)
            or item.filename in members
        ):
            raise ValueError("Archive contains an unsafe or duplicate member")
        members[item.filename] = item
    if MANIFEST not in members:
        raise ValueError("Archive has no inventory manifest")
    return members


def verify_zip(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        members = _members(archive)
        manifest = json.loads(archive.read(MANIFEST))
        if manifest.get("schema_version") != "jobapply-private-bundle-v1":
            raise ValueError("Unsupported inventory manifest")
        entries = manifest["files"]
        expected = {entry["archive_path"]: entry for entry in entries}
        if len(expected) != len(entries) or set(expected) != set(members) - {MANIFEST}:
            raise ValueError("Archive and manifest membership differ")
        for name, entry in expected.items():
            if members[name].file_size != entry["bytes"]:
                raise ValueError("Archive member size differs from the manifest")
            with archive.open(name) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != entry["sha256"]:
                raise ValueError("Archive member hash differs from the manifest")
    return {"files": len(entries), "bytes": sum(entry["bytes"] for entry in entries)}


def split_ciphertext(source: Path, directory: Path, name: str, *, part_bytes: int) -> dict:
    """Split encrypted bytes for Git LFS; publish no applicant metadata."""
    if (
        part_bytes < 1
        or not name
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in name)
    ):
        raise ValueError("Invalid part size or archive name")
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / f"{name}.json"
    if manifest_path.exists() or any(directory.glob(f"{name}.enc.part*")):
        raise ValueError("Archive output already exists")
    parts = []
    whole_hash = hashlib.sha256()
    with source.open("rb") as incoming:
        while incoming.peek(1):
            filename = f"{name}.enc.part{len(parts) + 1:03d}"
            digest = hashlib.sha256()
            remaining = part_bytes
            with (directory / filename).open("xb") as outgoing:
                while remaining and (chunk := incoming.read(min(CHUNK_BYTES, remaining))):
                    outgoing.write(chunk)
                    digest.update(chunk)
                    whole_hash.update(chunk)
                    remaining -= len(chunk)
            parts.append(
                {"file": filename, "bytes": part_bytes - remaining, "sha256": digest.hexdigest()}
            )
    if not parts:
        raise ValueError("Encrypted input is empty")
    manifest = {
        "schema_version": "jobapply-encrypted-parts-v1",
        "format": "jobapply-aes256gcm-v1",
        "parts": parts,
        "ciphertext_sha256": whole_hash.hexdigest(),
        "ciphertext_bytes": sum(part["bytes"] for part in parts),
    }
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    return manifest


def _join_parts(manifest_path: Path, output: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "jobapply-encrypted-parts-v1":
        raise ValueError("Unsupported encrypted-parts manifest")
    whole = hashlib.sha256()
    total = 0
    seen = set()
    with output.open("xb") as outgoing:
        for part in manifest["parts"]:
            name = part["file"]
            if (
                not isinstance(name, str)
                or not name
                or "/" in name
                or "\\" in name
                or ":" in name
                or name in {".", ".."}
                or name in seen
            ):
                raise ValueError("Invalid encrypted part name")
            seen.add(name)
            path = manifest_path.parent / name
            if path.is_symlink() or path.stat().st_size != part["bytes"]:
                raise ValueError("Encrypted part size or location is invalid")
            digest = hashlib.sha256()
            with path.open("rb") as incoming:
                while chunk := incoming.read(CHUNK_BYTES):
                    outgoing.write(chunk)
                    digest.update(chunk)
                    whole.update(chunk)
                    total += len(chunk)
            if digest.hexdigest() != part["sha256"]:
                raise ValueError("Encrypted part digest is invalid")
    if total != manifest["ciphertext_bytes"] or whole.hexdigest() != manifest["ciphertext_sha256"]:
        raise ValueError("Encrypted archive digest is invalid")


def inspect_archive(source: Path, key_path: Path, *, output_directory: Path | None = None) -> dict:
    if output_directory is not None and output_directory.exists():
        raise ValueError("Restore directory must not exist")
    # The system temp directory is local. No plaintext is written beside the public archive.
    with tempfile.TemporaryDirectory(prefix="jobapply-restore-") as temporary:
        if source.suffix == ".json":
            combined = Path(temporary) / "combined.enc"
            _join_parts(source, combined)
            source = combined
        plain = Path(temporary) / "payload.zip"
        decrypt_file(source, plain, key_path)
        result = verify_zip(plain)
        if output_directory is not None:
            output_directory.mkdir(parents=True, exist_ok=False)
            with zipfile.ZipFile(plain) as archive:
                for name in _members(archive):
                    destination = output_directory.joinpath(*PurePosixPath(name).parts)
                    if not destination.resolve().is_relative_to(output_directory.resolve()):
                        raise ValueError("Archive path escapes the restore directory")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as incoming, destination.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, CHUNK_BYTES)
    return {**result, "authenticated": True, "restored": output_directory is not None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("create-key")
    key.add_argument("--key-file", type=Path, required=True)
    split = commands.add_parser("split")
    split.add_argument("--input", type=Path, required=True)
    split.add_argument("--output-dir", type=Path, required=True)
    split.add_argument("--name", required=True)
    split.add_argument("--part-size-mib", type=int, default=512)
    for command in ("encrypt", "verify", "restore"):
        sub = commands.add_parser(command)
        sub.add_argument("--input", type=Path, required=True)
        sub.add_argument("--key-file", type=Path, required=True)
        if command == "encrypt":
            sub.add_argument("--output", type=Path, required=True)
        elif command == "restore":
            sub.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create-key":
            create_key(args.key_file)
            result = {"key_created": True, "key_disclosed": False}
        elif args.command == "split":
            result = split_ciphertext(
                args.input, args.output_dir, args.name, part_bytes=args.part_size_mib * 1024 * 1024
            )
        elif args.command == "encrypt":
            verify_zip(args.input)
            result = encrypt_file(args.input, args.output, args.key_file)
        else:
            result = inspect_archive(
                args.input, args.key_file, output_directory=getattr(args, "output_dir", None)
            )
    except Exception as error:
        parser.exit(2, f"encrypted archive: {type(error).__name__}; operation not completed\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
