"""Append or export local browser application activity; no browser or network access."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from batch_apply.activity import ActivityStore
from scripts.plan_browser_batch import _write


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("append", "export"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--ledger", default="runtime-data/browser-batch.sqlite3")
        sub.add_argument("--output")
        if command == "append":
            sub.add_argument("input", help="JSON {run_id,events:[...]} file")
        else:
            sub.add_argument("--run-id")
            sub.add_argument("--job-url")
    args = parser.parse_args(argv)
    try:
        store = ActivityStore(args.ledger)
        if args.command == "append":
            payload = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
            result = store.append_batch(payload)
        else:
            result = store.export(run_id=args.run_id, job_url=args.job_url)
        _write(result, args.output)
        return 0
    except (ValidationError, json.JSONDecodeError):
        parser.exit(2, "browser activity: invalid activity schema or JSON\n")
    except OSError:
        parser.exit(2, "browser activity: cannot access the requested local file\n")
    except ValueError as error:
        parser.exit(2, f"browser activity: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
