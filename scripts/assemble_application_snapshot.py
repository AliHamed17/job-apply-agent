"""Assemble an authenticated application delta over a base into a new directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath


def assemble(base: Path, delta: Path, output: Path) -> dict:
    base, delta, output = base.resolve(strict=True), delta.resolve(strict=True), output.resolve()
    if not base.is_dir() or not delta.is_dir() or output.exists():
        raise ValueError("Base and delta must exist; output must be new")
    if output.is_relative_to(base) or output.is_relative_to(delta):
        raise ValueError("Output must not be inside an input")
    manifest = json.loads((delta / "bundle-manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "jobapply-private-bundle-v1"
        or manifest.get("kind") != "verified-incremental-snapshot"
    ):
        raise ValueError("Unsupported private snapshot manifest")
    plan = []
    seen = set()
    for entry in manifest["complete_inventory"]:
        name = entry["archive_path"]
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or relative.as_posix() != name
            or ".." in relative.parts
            or "\\" in name
            or ":" in name
            or name.casefold() in seen
            or name == "bundle-manifest.json"
        ):
            raise ValueError("Unsafe or duplicate archive path")
        seen.add(name.casefold())
        if entry["storage"] not in {"base", "delta"}:
            raise ValueError("Unknown snapshot source")
        origin = delta if entry["storage"] == "delta" else base
        source = origin.joinpath(*relative.parts)
        for parent in (source, *source.parents):
            if parent == origin:
                break
            if parent.is_symlink():
                raise ValueError("Snapshot source may not contain symlinks")
        resolved = source.resolve(strict=True)
        if (
            not resolved.is_relative_to(origin)
            or not resolved.is_file()
            or resolved.stat().st_size != entry["bytes"]
        ):
            raise ValueError("Snapshot source is missing or changed")
        with resolved.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                raise ValueError("Snapshot source hash mismatch")
        plan.append((resolved, output.joinpath(*relative.parts), entry["bytes"], entry["sha256"]))
    output.mkdir(parents=True, exist_ok=False)
    for source, destination, size, digest in plan:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        with destination.open("rb") as stream:
            if (
                destination.stat().st_size != size
                or hashlib.file_digest(stream, "sha256").hexdigest() != digest
            ):
                raise ValueError("Snapshot changed during assembly")
    shutil.copyfile(delta / "bundle-manifest.json", output / "bundle-manifest.json")
    return {"assembled_files": len(plan), "output": str(output)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = assemble(args.base, args.delta, args.output)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        parser.exit(2, "snapshot assembly: invalid or inaccessible snapshot\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
