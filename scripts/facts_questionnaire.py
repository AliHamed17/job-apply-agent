"""Turn the facts that blocked jobs into one questionnaire, then into reviewed facts.

``build`` reads the ledger (read-only) and writes a questionnaire. The user fills in
answers, and ``apply`` validates them and writes ``reviewed-facts.json`` for
``scripts.export_verified_answer_cache --verified-facts``. Nothing is guessed: a blank
answer stays unknown, and only ``experience.<skill>_years`` can enter the answer cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from batch_apply.activity import reject_secrets
from batch_apply.facts_gap import (
    FIXABLE_KINDS,
    GapJob,
    domain_outcomes,
    experience_scope,
    mine_ledger,
    rank,
    skill_display,
    unclassified,
    unlock_estimate,
)

SCHEMA = "facts-questionnaire-v1"
PREFERENCES_SCHEMA = "confirmed-preferences-v1"
_EXPERIENCE_KEY = re.compile(r"experience\.([a-z][a-z0-9_]{0,60})_years")
_CONSENT_CHOICES = (
    "accept_required_terms_and_privacy_only",
    "never_accept_terms",
)
_PREFERENCE_FORMAT = {
    "availability.notice_or_start": "e.g. '30 days notice' or an exact start date",
    "workplace.hybrid_onsite_or_commute": "e.g. 'hybrid: yes, max 2 days; relocation: no'",
    "salary.expectation": "e.g. a monthly ILS range, or 'negotiable'",
    "travel.commitment": "e.g. 'up to 10% abroad' or 'no travel'",
    "certifications.training": "e.g. 'none' or the exact list you hold",
    "address.postal_code": "your postal code, only if you are comfortable sharing it",
}
_MAX_ANSWER = 200


def _load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Questionnaire must be a JSON object")
    return data


def build_questionnaire(jobs: list[GapJob]) -> dict:
    rows = rank(jobs)
    stuck = sum(1 for job in jobs if job.status in {"blocked", "skipped"})
    answerable = {row.key for row in rows if row.kind in FIXABLE_KINDS}
    experience = [
        {
            "key": row.key,
            "skill": skill_display(row.key.removeprefix("experience.").removesuffix("_years")),
            "scope": experience_scope(row.key.removeprefix("experience.").removesuffix("_years")),
            "jobs": row.jobs,
            "also_blocked_by_other_gaps": row.jobs - row.sole,
            "example_employers": list(row.companies),
            "years": None,
        }
        for row in rows
        if row.kind == "experience"
    ]
    preferences = [
        {
            "key": row.key,
            "title": row.title,
            "jobs": row.jobs,
            "example_employers": list(row.companies),
            "format": _PREFERENCE_FORMAT.get(row.key, "free text"),
            "answer": None,
        }
        for row in rows
        if row.kind == "fact"
    ]
    consent = next((row for row in rows if row.kind == "consent"), None)
    return {
        "schema_version": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "how_to_answer": (
            "Fill only what is true. Leave anything else null. Numbers are whole or "
            "decimal years. Only 'experience' answers can enter the answer cache today; "
            "'preferences' and 'consent_policy' are stored but unused until you approve "
            "automation answering that category."
        ),
        "summary": {
            "blocked_or_skipped_jobs": stuck,
            "unclassified": unclassified(jobs),
            "unblocked_if_all_answered": unlock_estimate(jobs, answerable),
            "note": (
                "Keyword-based estimate. A job counts only if every one of its gaps is a "
                "fact or consent you can supply."
            ),
        },
        "experience": experience,
        "preferences": preferences,
        "consent_policy": {
            "key": "consent.terms_or_privacy",
            "jobs": consent.jobs if consent else 0,
            "allowed_answers": list(_CONSENT_CHOICES),
            "answer": None,
        },
        "not_fixable_by_facts": [
            {"key": row.key, "kind": row.kind, "title": row.title, "jobs": row.jobs}
            for row in rows
            if row.kind not in FIXABLE_KINDS
        ],
        "domains": domain_outcomes(jobs)[:12],
    }


def _jobs(count: int) -> str:
    return f"{count} job" + ("" if count == 1 else "s")


def render_markdown(questionnaire: dict) -> str:
    summary = questionnaire["summary"]
    lines = [
        "# Facts that blocked applications",
        "",
        f"Blocked or skipped jobs: **{summary['blocked_or_skipped_jobs']}**. "
        "Answering everything below could unblock about "
        f"**{summary['unblocked_if_all_answered']}**. ({summary['note']})",
        "",
        "## Experience (usable by the answer cache today)",
        "",
        "| Skill | Jobs | Years |",
        "|---|---:|---|",
    ]
    for item in questionnaire["experience"]:
        lines.append(f"| {item['skill']} | {item['jobs']} | |")
    lines += ["", "## Preferences (need your approval before automation may use them)", ""]
    for item in questionnaire["preferences"]:
        lines.append(f"- **{item['title']}** ({_jobs(item['jobs'])}): {item['format']}")
    consent = questionnaire["consent_policy"]
    lines += [
        "",
        f"## Consent checkboxes ({_jobs(consent['jobs'])})",
        "",
        "Choose one: " + " or ".join(f"`{choice}`" for choice in consent["allowed_answers"]) + ".",
        "",
        "## Not fixable by facts",
        "",
        "| Gap | Jobs |",
        "|---|---:|",
    ]
    for item in questionnaire["not_fixable_by_facts"]:
        lines.append(f"| {item['title']} | {item['jobs']} |")
    lines += [
        "",
        "Fill the answers in `facts-questionnaire.json`, then run "
        "`python -m scripts.facts_questionnaire apply`.",
        "",
    ]
    return "\n".join(lines)


def _years(value: object, where: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{where}: years must be a number")
    if not 0 <= value <= 80:
        raise ValueError(f"{where}: years must be between 0 and 80")
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: answer must be non-empty text")
    text = value.strip()
    if len(text) > _MAX_ANSWER:
        raise ValueError(f"{where}: answer is longer than {_MAX_ANSWER} characters")
    if re.search(r"\d{9,}", text):
        raise ValueError(f"{where}: long digit runs (ID numbers) are not stored")
    return text


def apply_questionnaire(questionnaire_path: Path, out_dir: Path) -> dict[str, Path]:
    """Validate answers; write an immutable proof copy, reviewed facts and preferences."""
    raw = questionnaire_path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA:
        raise ValueError("Not a facts questionnaire (wrong schema_version)")
    reject_secrets(data)
    facts: list[tuple[str, str, float | int]] = []
    seen: set[str] = set()
    for index, item in enumerate(data.get("experience") or []):
        if not isinstance(item, dict) or item.get("years") is None:
            continue
        match = _EXPERIENCE_KEY.fullmatch(str(item.get("key", "")))
        if not match:
            raise ValueError(f"experience[{index}]: key must look like experience.<skill>_years")
        key = str(item["key"])
        if key in seen:
            raise ValueError(f"experience[{index}]: duplicate key")
        seen.add(key)
        slug = match.group(1)
        facts.append((key, experience_scope(slug), _years(item["years"], f"experience[{index}]")))
    preferences: dict[str, str] = {}
    for index, item in enumerate(data.get("preferences") or []):
        if isinstance(item, dict) and item.get("answer") is not None:
            preferences[str(item.get("key"))] = _text(item["answer"], f"preferences[{index}]")
    consent = data.get("consent_policy") or {}
    if consent.get("answer") is not None:
        if consent["answer"] not in _CONSENT_CHOICES:
            raise ValueError("consent_policy: answer must be one of " + ", ".join(_CONSENT_CHOICES))
        preferences["consent.terms_or_privacy"] = consent["answer"]
    if not facts and not preferences:
        raise ValueError("No answers found; nothing to apply")

    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    proof = out_dir / f"facts-questionnaire.confirmed.{digest[:12]}.json"
    if not proof.exists():
        proof.write_bytes(raw)
    paths = {"proof": proof}
    if facts:
        manifest = {
            "facts": [
                {
                    "id": "confirmed-" + key.replace(".", "-"),
                    "key": key,
                    "value": value,
                    "scope": scope,
                    "reviewed": True,
                    "inherited_prefill": False,
                    "source_type": "user_confirmation",
                    "proofs": [{"path": str(proof.resolve()), "sha256": digest}],
                }
                for key, scope, value in facts
            ]
        }
        paths["reviewed_facts"] = out_dir / "reviewed-facts.json"
        paths["reviewed_facts"].write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if preferences:
        paths["preferences"] = out_dir / "confirmed-preferences.json"
        paths["preferences"].write_text(
            json.dumps(
                {
                    "schema_version": PREFERENCES_SCHEMA,
                    "source_type": "user_confirmation",
                    "proof_sha256": digest,
                    "used_by_answer_cache": False,
                    "answers": preferences,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return paths


def _print_report(jobs: list[GapJob], examples: bool) -> None:
    rows = rank(jobs)
    print(f"blocked_or_skipped={sum(j.status in {'blocked', 'skipped'} for j in jobs)}")
    print(f"unclassified={unclassified(jobs)}")
    print(f"{'gap':44}{'kind':11}{'jobs':>5}{'sole':>5}")
    for row in rows:
        suffix = "  " + ", ".join(row.companies) if examples else ""
        print(f"{row.key:44}{row.kind:11}{row.jobs:5}{row.sole:5}{suffix}")
    answerable = {row.key for row in rows if row.kind in FIXABLE_KINDS}
    print(f"unblocked_if_all_fixable_answered={unlock_estimate(jobs, answerable)}")
    for item in domain_outcomes(jobs)[:8]:
        print("domain", json.dumps(item, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build")
    report_cmd = sub.add_parser("report")
    for cmd in (build_cmd, report_cmd):
        cmd.add_argument("--ledger", default="runtime-data/browser-batch.sqlite3")
    build_cmd.add_argument("--out-dir", default="runtime-data/facts")
    report_cmd.add_argument("--examples", action="store_true")
    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("--questionnaire", default="runtime-data/facts/facts-questionnaire.json")
    apply_cmd.add_argument("--out-dir", default="runtime-data/facts")
    args = parser.parse_args(argv)
    try:
        if args.command == "apply":
            paths = apply_questionnaire(Path(args.questionnaire), Path(args.out_dir))
            for name, path in paths.items():
                print(f"{name}={path}")
            return 0
        jobs = mine_ledger(args.ledger)
        if args.command == "report":
            _print_report(jobs, args.examples)
            return 0
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        questionnaire = build_questionnaire(jobs)
        target = out / "facts-questionnaire.json"
        if target.exists():
            target = out / f"facts-questionnaire.{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
            print("existing questionnaire kept; writing a new file", file=sys.stderr)
        target.write_text(json.dumps(questionnaire, indent=2) + "\n", encoding="utf-8")
        target.with_suffix(".md").write_text(render_markdown(questionnaire), encoding="utf-8")
        print(f"questionnaire={target}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
