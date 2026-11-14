"""Export a private answer/CV evidence cache without changing the application ledger."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from batch_apply.answer_cache import build_answer_cache


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default="runtime-data/browser-batch.sqlite3")
    parser.add_argument("--overlay", default="runtime-data/verified-profile-overlay.json")
    parser.add_argument("--profile", default="user_profile.yaml")
    parser.add_argument("--cv-dir", required=True)
    parser.add_argument("--verified-facts")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        output = Path(args.output).resolve()
        inputs = [args.ledger, args.overlay, args.profile]
        if args.verified_facts:
            inputs.append(args.verified_facts)
        if output in {Path(path).resolve() for path in inputs} or output.suffix.lower() != ".json":
            raise ValueError("Cache output must be a separate JSON file")
        cache = build_answer_cache(
            ledger_path=args.ledger,
            overlay_path=args.overlay,
            profile_path=args.profile,
            cv_directory=args.cv_dir,
            verified_facts_path=args.verified_facts,
        )
        if output in {Path(source.path).resolve() for source in cache.source_files}:
            raise ValueError("Cache output cannot overwrite evidence sources")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(cache.model_dump_json(indent=2) + "\n", encoding="utf-8")
        # No contact values, answers, source text or complete cache in process logs.
        print(
            json.dumps(
                {
                    "status": "exported",
                    "facts": len(cache.facts),
                    "cv_routes": len(cache.cv_routes),
                    "ledger_modified": False,
                }
            )
        )
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, yaml.YAMLError) as error:
        parser.exit(2, f"answer cache: {type(error).__name__}; no ledger changes\n")


if __name__ == "__main__":
    raise SystemExit(main())
