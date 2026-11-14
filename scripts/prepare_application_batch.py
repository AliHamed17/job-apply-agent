"""Build bounded, private application packets without LLM or browser calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from batch_apply.urls import canonical_job_url, job_key
from batch_apply.workflow import ObservedApplication, prepare_packet, write_private_json

MAX_INPUT_BYTES = 20_000_000


def _read_applications(path: Path) -> list[object]:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Input exceeds the 20 MB safety limit")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    applications = payload.get("applications") if isinstance(payload, dict) else payload
    if not isinstance(applications, list) or not applications:
        raise ValueError("Input must be a nonempty list or an object with applications")
    return applications


def _private_output_dir(raw: str, cv_directory: str | Path) -> Path:
    runtime_root = Path("runtime-data").resolve(strict=True)
    output = Path(raw).resolve()
    if output == runtime_root or not output.is_relative_to(runtime_root):
        raise ValueError("Output directory must be a new child of runtime-data")
    cv_root = Path(cv_directory).resolve(strict=True)
    if output == cv_root or output.is_relative_to(cv_root) or cv_root.is_relative_to(output):
        raise ValueError("Output directory must not overlap the CV directory")
    if output.exists():
        raise ValueError("Output directory already exists; choose a fresh run directory")
    return output


def _knowledge(path: Path) -> dict:
    knowledge = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(knowledge, dict):
        raise ValueError("Application knowledge must be a JSON object")
    if knowledge.get("schema_version") != "application-workflow-knowledge-v1":
        raise ValueError("Unsupported application knowledge version")
    required = {"ledger_path", "answer_cache_path", "cv_directory", "employer_policy_path"}
    if not required.issubset(knowledge):
        raise ValueError("Application knowledge is missing required source paths")
    if any(not isinstance(knowledge[key], str) or not knowledge[key] for key in required):
        raise ValueError("Application knowledge has invalid source paths")
    protected_sources = knowledge.get("protected_source_paths", [])
    if not isinstance(protected_sources, list) or any(
        not isinstance(path, str) or not path for path in protected_sources
    ):
        raise ValueError("Application knowledge has invalid protected sources")
    return knowledge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Private observed-applications JSON")
    parser.add_argument("--knowledge", default="runtime-data/application-knowledge.json")
    parser.add_argument("--output-dir", required=True, help="New private runtime-data subdirectory")
    parser.add_argument("--max-applications", type=int, default=50)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_applications <= 200:
            raise ValueError("max-applications must be between 1 and 200")
        input_path = Path(args.input).resolve(strict=True)
        knowledge_path = Path(args.knowledge).resolve(strict=True)
        raw_applications = _read_applications(input_path)
        if len(raw_applications) > args.max_applications:
            raise ValueError(
                f"Input has {len(raw_applications)} records; split it into batches of "
                f"at most {args.max_applications}"
            )
        knowledge = _knowledge(knowledge_path)
        output_dir = _private_output_dir(args.output_dir, knowledge["cv_directory"])
        output_dir.mkdir(parents=True, exist_ok=False)

        protected_sources = [
            input_path,
            knowledge_path,
            Path(knowledge["ledger_path"]),
            Path(knowledge["answer_cache_path"]),
            Path(knowledge["employer_policy_path"]),
            *(Path(path) for path in knowledge.get("protected_source_paths", [])),
        ]
        seen: dict[str, int] = {}
        packets: list[dict] = []
        action_counts: Counter[str] = Counter()

        for index, raw in enumerate(raw_applications, start=1):
            try:
                application = ObservedApplication.model_validate(raw)
                canonical = canonical_job_url(application.job.url)
                identifier = job_key(canonical)
                if canonical in seen:
                    action_counts["duplicate_in_batch"] += 1
                    packets.append(
                        {
                            "index": index,
                            "job_id": identifier,
                            "next_action": "duplicate_in_batch",
                            "duplicate_of_index": seen[canonical],
                        }
                    )
                    continue
                packet = prepare_packet(
                    application,
                    ledger_path=knowledge["ledger_path"],
                    cache_path=knowledge["answer_cache_path"],
                    cv_directory=knowledge["cv_directory"],
                    policy_path=knowledge["employer_policy_path"],
                )
                filename = f"{index:03d}-{hashlib.sha256(canonical.encode()).hexdigest()[:12]}.json"
                packet_path = output_dir / filename
                write_private_json(packet_path, packet, inputs=protected_sources)
                seen[canonical] = index
                action = packet["next_action"]
                action_counts[action] += 1
                packets.append(
                    {
                        "index": index,
                        "job_id": identifier,
                        "next_action": action,
                        "packet": str(packet_path),
                    }
                )
            except Exception as error:
                # Isolate a bad job; the batch still reports an error and returns nonzero.
                action_counts["input_or_prepare_error"] += 1
                packets.append(
                    {
                        "index": index,
                        "next_action": "input_or_prepare_error",
                        "error_type": type(error).__name__,
                    }
                )

        summary = {
            "schema_version": "application-batch-preparation-v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "input_count": len(raw_applications),
            "unique_job_count": len(seen),
            "action_counts": dict(action_counts),
            "llm_calls": 0,
            "network_requests": 0,
            "browser_actions": 0,
            "ledger_mutations": 0,
            "packets": packets,
        }
        summary_path = output_dir / "summary.json"
        write_private_json(summary_path, summary, inputs=protected_sources)
        has_errors = action_counts["input_or_prepare_error"] > 0
        print(
            json.dumps(
                {
                    "status": "batch_prepared_with_errors" if has_errors else "batch_prepared",
                    "input_count": len(raw_applications),
                    "unique_job_count": len(seen),
                    "action_counts": dict(action_counts),
                    "llm_calls": 0,
                    "network_requests": 0,
                    "browser_actions": 0,
                    "ledger_mutations": 0,
                    "summary": str(summary_path),
                },
                ensure_ascii=True,
            )
        )
        return 1 if has_errors else 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        message = (
            f"application batch preparation: {type(error).__name__}; no applications submitted\n"
        )
        parser.exit(2, message)


if __name__ == "__main__":
    raise SystemExit(main())
