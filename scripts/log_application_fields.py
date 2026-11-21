"""Record local, structured application fields; performs no browser or network actions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from batch_apply.field_audit import append_field_captures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Private JSON file containing observed field captures")
    parser.add_argument("--ledger", default="runtime-data/browser-batch.sqlite3")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
        result = append_field_captures(args.ledger, payload)
    except (ValidationError, json.JSONDecodeError, ValueError):
        parser.exit(2, "application fields: invalid or prohibited field data\n")
    except OSError:
        parser.exit(2, "application fields: cannot access the requested local file\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
