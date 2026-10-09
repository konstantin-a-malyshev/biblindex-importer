"""Command line entry point.

    python -m biblindex_importer export --book John
    python -m biblindex_importer export --book Mt --with-works --output out/matthew.csv
    python -m biblindex_importer books
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from .books import BookNotFoundError, fetch_books, find_book
from .cache import ResponseCache
from .client import ApiError, BiblIndexClient
from .config import ConfigError, Settings
from .exporter import BookExporter, write_csv

log = logging.getLogger("biblindex_importer")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="biblindex-import",
        description="Export references to Bible verses from the BiblIndex API to CSV.",
    )
    parser.add_argument("--env-file", default=".env", help="Credentials file (default: .env).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Export all quotations of one Bible book.")
    export.add_argument(
        "--book",
        required=True,
        help='Book name, abbreviation, code or id, e.g. "John", "Jn", "43N" or 50.',
    )
    export.add_argument(
        "--bible", type=int, help="Bible id (default: BIBLINDEX_BIBLE_ID from .env, else 8)."
    )
    export.add_argument(
        "--output", type=Path, help="CSV path (default: output/<book>_quotations.csv)."
    )
    export.add_argument(
        "--with-works",
        action="store_true",
        help="Also add the quoting work, edition and position in the work (slow: see README).",
    )
    export.add_argument(
        "--limit", type=int, help="Export only the first N quotations (for a trial run)."
    )
    export.add_argument(
        "--refresh", action="store_true", help="Ignore cached API responses and re-download."
    )

    sub.add_parser("books", help="List Bible books with their ids and abbreviations.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        settings = Settings.from_env(args.env_file)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    cache = ResponseCache(settings.cache_path, ttl_seconds=settings.cache_ttl_days * 86400)
    if getattr(args, "refresh", False):
        cache.clear()
    client = BiblIndexClient(settings, cache=cache)
    try:
        if args.command == "books":
            return _list_books(client)
        return _export(client, settings, args)
    except (ApiError, BookNotFoundError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted. Responses fetched so far are cached; rerun to resume.")
        return 130
    finally:
        stats = client.auth_limiter.stats
        log.info(
            "API usage: %d rate-limited requests (%d answered 429), %d public requests, "
            "%d cache hits, %.0fs spent pacing.",
            stats.requests,
            stats.throttled,
            client.public_limiter.stats.requests,
            client.cache_hits,
            stats.waited_seconds + client.public_limiter.stats.waited_seconds,
        )
        cache.close()


def _list_books(client: BiblIndexClient) -> int:
    for book in fetch_books(client):
        print(f"{book.id:>4}  {book.abbreviation:<10} {book.code:<5} {book.name}")
    return 0


def _export(client: BiblIndexClient, settings: Settings, args: argparse.Namespace) -> int:
    if not settings.has_credentials:
        log.error("Set BIBLINDEX_CLIENT_ID and BIBLINDEX_CLIENT_SECRET in %s.", args.env_file)
        return 2
    book = find_book(fetch_books(client), args.book)
    bible_id = args.bible if args.bible is not None else settings.bible_id
    output = args.output or Path("output") / f"{book.slug or book.id}_quotations.csv"

    result = BookExporter(client, bible_id).export(
        book,
        with_works=args.with_works,
        limit=args.limit,
    )
    write_csv(result.rows, output, with_works=args.with_works)
    log.info(
        "Wrote %d rows (%d quotations of %s) to %s.",
        len(result.rows),
        result.quotations,
        book.name,
        output,
    )
    if args.with_works:
        log.info("%d quotations without a resolved work/position.", result.unresolved_works)
    return 0


if __name__ == "__main__":
    sys.exit(main())
