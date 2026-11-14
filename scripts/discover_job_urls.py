"""Build a cached, read-only discovery manifest from saved job URLs."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

import structlog

from discovery.fast_discovery import DiscoveryBatch, read_saved_urls, write_json_atomic


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        type=Path,
        default=[],
        help="Saved WhatsApp text or lead/tab/message JSON; repeatable",
    )
    parser.add_argument("--url", action="append", default=[], help="Explicit public job/board URL")
    parser.add_argument("--cache", type=Path, default=Path("runtime-data/discovery/cache.json"))
    parser.add_argument("--output", type=Path, default=Path("runtime-data/discovery/latest.json"))
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Opt in an exact public company careers hostname; repeatable",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--per-host", type=int, default=2)
    parser.add_argument("--cache-ttl", type=float, default=21600, help="Seconds; 0 revalidates")
    parser.add_argument("--timeout", type=float, default=15, help="Per-request timeout in seconds")
    args = parser.parse_args()
    # Keep stdout machine-readable even when an existing parser emits diagnostics.
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    if not args.input and not args.url:
        parser.error("Provide --input or --url")
    destinations = {args.cache.resolve(), args.output.resolve()}
    if len(destinations) != 2 or any(path.resolve() in destinations for path in args.input):
        parser.error("Input files, cache, and output must use different paths")
    urls = list(args.url)
    try:
        for path in args.input:
            urls.extend(read_saved_urls(path.read_text(encoding="utf-8-sig")))
        batch = DiscoveryBatch(
            cache_path=args.cache,
            allowed_hosts=args.allow_host,
            concurrency=args.concurrency,
            per_host=args.per_host,
            ttl_seconds=args.cache_ttl,
            timeout_seconds=args.timeout,
        )
        report = asyncio.run(batch.run(urls))
        write_json_atomic(args.output, report)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "input_count": report["input_count"],
                "duplicate_count": report["duplicate_count"],
                "cache_hits": sum(row["cache_hit"] for row in report["results"]),
                "statuses": dict(Counter(row["status"] for row in report["results"])),
                "output": str(args.output),
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
