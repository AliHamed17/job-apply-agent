"""Refresh application knowledge or prepare the next observed form for the LLM."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from batch_apply.answer_cache import AnswerCache, build_answer_cache
from batch_apply.workflow import (
    ObservedApplication,
    build_knowledge,
    prepare_packet,
    write_private_json,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", help="Read saved evidence; rebuild reusable knowledge")
    refresh.add_argument("--ledger", default="runtime-data/browser-batch.sqlite3")
    refresh.add_argument("--overlay", default="runtime-data/verified-profile-overlay.json")
    refresh.add_argument("--profile", default="user_profile.yaml")
    refresh.add_argument("--verified-facts")
    refresh.add_argument("--cv-dir", required=True)
    refresh.add_argument("--policy", default="runtime-data/employer-policy.json")
    refresh.add_argument("--cache", default="runtime-data/verified-answer-cache.json")
    refresh.add_argument("--output", default="runtime-data/application-knowledge.json")
    prepare = commands.add_parser(
        "prepare", help="Resolve observed job/form into a live-review packet"
    )
    prepare.add_argument("--knowledge", default="runtime-data/application-knowledge.json")
    prepare.add_argument("--input", required=True)
    prepare.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            verified_facts = args.verified_facts
            if not verified_facts and Path(args.cache).is_file():
                previous = AnswerCache.model_validate_json(
                    Path(args.cache).read_text(encoding="utf-8-sig")
                )
                manifests = {
                    fact.source_path for fact in previous.facts if fact.basis == "reviewed_evidence"
                }
                if len(manifests) > 1:
                    raise ValueError("Multiple reviewed manifests require an explicit choice")
                verified_facts = next(iter(manifests), None)
            conventional = Path(args.cache).parent / "verified-answer-facts.json"
            if not verified_facts and conventional.is_file():
                verified_facts = str(conventional)
            inputs = [args.ledger, args.overlay, args.profile, args.policy]
            if verified_facts:
                inputs.append(verified_facts)
            if Path(args.output).resolve() == Path(args.cache).resolve():
                raise ValueError("Knowledge and answer cache must use separate files")
            # Validate both destinations before writing either file.
            for output in [args.output, args.cache]:
                if Path(output).suffix.lower() != ".json" or Path(output).resolve() in {
                    Path(path).resolve() for path in inputs
                }:
                    raise ValueError("Outputs must be separate JSON files")
            cache = build_answer_cache(
                ledger_path=args.ledger,
                overlay_path=args.overlay,
                profile_path=args.profile,
                cv_directory=args.cv_dir,
                verified_facts_path=verified_facts,
            )
            inputs.extend(source.path for source in cache.source_files)
            for output in [args.output, args.cache]:
                if Path(output).resolve() in {Path(path).resolve() for path in inputs}:
                    raise ValueError("Output would overwrite a verified source")
            write_private_json(args.cache, cache.model_dump(mode="json"), inputs=inputs)
            knowledge = build_knowledge(
                ledger_path=args.ledger,
                cache_path=args.cache,
                cv_directory=args.cv_dir,
                policy_path=args.policy,
            )
            write_private_json(args.output, knowledge, inputs=[*inputs, args.cache])
            print(
                json.dumps(
                    {
                        "status": "knowledge_refreshed",
                        "facts": len(cache.facts),
                        "cv_routes": len(cache.cv_routes),
                        "recorded_steps": knowledge["activity_event_count"],
                        "ledger_modified": False,
                    }
                )
            )
        else:
            knowledge = json.loads(Path(args.knowledge).read_text(encoding="utf-8-sig"))
            if knowledge.get("schema_version") != "application-workflow-knowledge-v1":
                raise ValueError("Unsupported application knowledge version")
            application = ObservedApplication.model_validate_json(
                Path(args.input).read_text(encoding="utf-8-sig")
            )
            inputs = [
                args.knowledge,
                args.input,
                knowledge["ledger_path"],
                knowledge["answer_cache_path"],
                knowledge["employer_policy_path"],
                *knowledge.get("protected_source_paths", []),
            ]
            packet = prepare_packet(
                application,
                ledger_path=knowledge["ledger_path"],
                cache_path=knowledge["answer_cache_path"],
                cv_directory=knowledge["cv_directory"],
                policy_path=knowledge["employer_policy_path"],
            )
            # The cache can be refreshed independently after the knowledge snapshot.
            # Protect its current sources too, including a newly introduced manifest.
            live_cache = AnswerCache.model_validate_json(
                Path(knowledge["answer_cache_path"]).read_text(encoding="utf-8-sig")
            )
            inputs.extend(source.path for source in live_cache.source_files)
            write_private_json(args.output, packet, inputs=inputs)
            print(
                json.dumps(
                    {
                        "status": packet["next_action"],
                        "fields": len(packet["fields"]),
                        "browser_actions_performed": False,
                        "ledger_modified": False,
                    }
                )
            )
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(2, f"application workflow: {type(error).__name__}; no browser actions\n")


if __name__ == "__main__":
    raise SystemExit(main())
