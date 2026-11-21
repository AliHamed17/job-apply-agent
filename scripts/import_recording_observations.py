"""Import reviewed recording observations into a local application ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from batch_apply.recording_observations import import_review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", help="Path to private, manually reviewed JSON")
    parser.add_argument("--ledger", default="runtime-data/browser-batch.sqlite3")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.review).read_text(encoding="utf-8-sig"))
        result = import_review(payload, args.ledger)
    except (OSError, ValueError, TypeError, OverflowError, ValidationError):
        parser.exit(2, "Recording review could not be validated or imported.\n")
    print(
        json.dumps(
            {
                "inserted": len(result["inserted"]),
                "duplicates": len(result["duplicate_ids"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
