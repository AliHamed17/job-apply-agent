"""Plan explicit browser tabs and record observed outcomes; never drive a browser."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from batch_apply.catalog import choose_cv_file, cv_catalog, recommend_cv, verify_cv
from batch_apply.ledger import BatchLedger, cv_selection_allowed
from batch_apply.models import BatchStatus, ObservedEvidence, TabSnapshot
from batch_apply.urls import canonical_job_url

DEFAULT_LEDGER = "runtime-data/browser-batch.sqlite3"


def _read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_snapshot(path: str | Path) -> list[TabSnapshot]:
    payload = _read_json(path)
    tabs = payload.get("tabs") if isinstance(payload, dict) else payload
    if not isinstance(tabs, list):
        raise ValueError("Snapshot must be a tab list or an object with a tabs list")
    return [TabSnapshot.model_validate(tab) for tab in tabs]


def _selections(path: str | None) -> dict[str, dict]:
    if not path:
        return {}
    entries = _read_json(path)
    if not isinstance(entries, list):
        raise ValueError("Selections must be a list of {url, cv_file, reason} objects")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            entry.get(k) for k in ("url", "cv_file", "reason")
        ):
            raise ValueError("Each selection needs url, cv_file and reason")
        canonical = canonical_job_url(entry["url"])
        if canonical in result:
            raise ValueError("A job cannot have multiple explicit CV selections")
        result[canonical] = entry
    return result


def _write(payload: dict, output: str | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
    # Export files stay UTF-8. Windows consoles/pipes can instead expose a
    # legacy encoding; JSON escapes preserve every character without changing
    # global stdout configuration or reporting failure after a successful write.
    console_rendered = rendered
    encoding = getattr(sys.stdout, "encoding", None)
    if encoding:
        try:
            console_rendered.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            console_rendered = json.dumps(payload, ensure_ascii=True, indent=2)
    try:
        print(console_rendered)
    except UnicodeEncodeError:
        # Some host-provided text streams have no encoding attribute.
        print(json.dumps(payload, ensure_ascii=True, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "plan",
        "list",
        "record",
        "claim",
        "select-cv",
        "verify-cv",
        "resume-blocked",
        "reconsider-skipped",
        "check-employer",
        "pre-submit-check",
    ):
        sub = subparsers.add_parser(command)
        sub.add_argument("--ledger", default=DEFAULT_LEDGER)
        sub.add_argument("--employer-policy", help="Local employer exclusion policy JSON")
        sub.add_argument("--output")
        if command == "plan":
            sub.add_argument("snapshot", help="JSON tab inventory captured from the browser UI")
            sub.add_argument("--cv-dir", required=True)
            sub.add_argument("--selections", help="Explicit selections made by the LLM runner")
        if command in {
            "record",
            "select-cv",
            "verify-cv",
            "resume-blocked",
            "reconsider-skipped",
            "check-employer",
            "pre-submit-check",
        }:
            sub.add_argument("--url", required=True, help="Original URL, canonical URL, or job id")
        if command in {"check-employer", "pre-submit-check"}:
            sub.add_argument("--observed-employer", default="")
            sub.add_argument("--observed-url", action="append", default=[])
        if command == "record":
            sub.add_argument(
                "--status",
                required=True,
                choices=[
                    state.value
                    for state in BatchStatus
                    if state
                    not in {BatchStatus.DISCOVERED, BatchStatus.PREPARED, BatchStatus.IN_PROGRESS}
                ],
            )
        if command in {"record", "reconsider-skipped"}:
            sub.add_argument(
                "--evidence-json",
                required=True,
                help="ObservedEvidence JSON; never passwords or verification codes",
            )
        if command in {"select-cv", "resume-blocked", "reconsider-skipped"}:
            sub.add_argument("--cv-dir", required=True)
            sub.add_argument("--file", required=True)
        if command == "select-cv":
            sub.add_argument("--reason", required=True)
            sub.add_argument("--source", default="llm_browser_review")
        if command in {"resume-blocked", "reconsider-skipped"}:
            sub.add_argument(
                "--resolution",
                required=True,
                help="Current user authorization or confirmed facts; never identifiers or secrets",
            )
    catalog = subparsers.add_parser("catalog")
    catalog.add_argument("--cv-dir", required=True)
    catalog.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            _write(
                {
                    "directory": str(Path(args.cv_dir).resolve()),
                    "files": [path.name for path in cv_catalog(args.cv_dir)],
                },
                args.output,
            )
            return 0
        ledger = BatchLedger(args.ledger, employer_policy_path=args.employer_policy)
        if args.command == "plan":
            tabs = read_snapshot(args.snapshot)
            selections = _selections(args.selections)
            explicit = {
                url: choose_cv_file(
                    args.cv_dir,
                    entry["cv_file"],
                    source="llm_browser_review",
                    reason=entry["reason"],
                )
                for url, entry in selections.items()
            }
            inventory_urls = {canonical_job_url(tab.url) for tab in tabs}
            if not set(explicit).issubset(inventory_urls):
                raise ValueError(
                    "Every explicit selection must belong to the supplied tab inventory"
                )
            imported = ledger.import_tabs(tabs)
            for tab in tabs:
                canonical = canonical_job_url(tab.url)
                job = ledger.get(canonical)
                if not cv_selection_allowed(job):
                    continue
                if canonical in explicit:
                    ledger.select_cv(canonical, explicit[canonical])
                elif job.status == BatchStatus.DISCOVERED:
                    choice, reason = recommend_cv(tab, args.cv_dir)
                    ledger.prepare(canonical, choice, reason)
            payload = ledger.export()
            payload["import"] = imported
        elif args.command in {"check-employer", "pre-submit-check"}:
            checker = (
                ledger.pre_submit_check
                if args.command == "pre-submit-check"
                else ledger.check_employer
            )
            payload = checker(
                args.url,
                observed_employer=args.observed_employer,
                observed_urls=args.observed_url,
            )
            _write(payload, args.output)
            return 0 if payload["allowed"] else 1
        elif args.command == "record":
            evidence = ObservedEvidence.model_validate(_read_json(args.evidence_json))
            payload = ledger.record(args.url, BatchStatus(args.status), evidence).model_dump(
                mode="json"
            )
        elif args.command == "claim":
            job = ledger.claim_next()
            payload = {"job": job.model_dump(mode="json") if job else None}
        elif args.command == "select-cv":
            choice = choose_cv_file(args.cv_dir, args.file, source=args.source, reason=args.reason)
            payload = ledger.select_cv(args.url, choice).model_dump(mode="json")
        elif args.command == "resume-blocked":
            resolution = args.resolution.strip()
            if not resolution:
                raise ValueError("A nonempty blocker resolution note is required")
            choice = choose_cv_file(
                args.cv_dir, args.file, source="explicit_blocker_resume", reason=resolution
            )
            payload = ledger.resume_blocked(args.url, choice, resolution).model_dump(mode="json")
        elif args.command == "reconsider-skipped":
            resolution = args.resolution.strip()
            if not resolution:
                raise ValueError("A nonempty skip resolution note is required")
            choice = choose_cv_file(
                args.cv_dir, args.file, source="explicit_skip_reconsideration", reason=resolution
            )
            evidence = ObservedEvidence.model_validate(_read_json(args.evidence_json))
            payload = ledger.reconsider_skipped(args.url, choice, resolution, evidence).model_dump(
                mode="json"
            )
        elif args.command == "verify-cv":
            job = ledger.get(args.url)
            valid = bool(job.selected_cv and verify_cv(job.selected_cv))
            payload = {
                "job_id": job.job_id,
                "valid": valid,
                "selected_cv": job.selected_cv.model_dump() if job.selected_cv else None,
            }
            _write(payload, args.output)
            return 0 if valid else 1
        else:
            payload = ledger.export()
        _write(payload, args.output)
        return 0
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"browser batch: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
