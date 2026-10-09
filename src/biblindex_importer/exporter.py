"""Collect the quotations of one Bible book and flatten them into CSV rows.

Data model (see the API docs): a *quotation* points to an *extract* (the quoted
biblical passage), which is built from one or more *sequences* (spans with exact
book/chapter/verse coordinates in a given Bible). One CSV row is written per
quotation and per sequence of its extract that starts in the requested book.

All three collections accept the same ``bible`` + ``startBook`` filter, so the
core export costs only a few paginated listings (100 items per request).

Reading sequences needs a separate grant from BiblIndex. Without it, the
coordinates are parsed from the extract's reference text (``Jn 3:16-18``); the
``coordinates_from`` column records which source each row used.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .books import Book
from .client import ApiError, BiblIndexClient, PermissionDeniedError
from .references import Span, parse_reference, same_book

log = logging.getLogger(__name__)

FROM_SEQUENCE = "sequence"
FROM_REFERENCE = "reference"

PAGE_SIZE = 100  # the API's maximum itemsPerPage

BASE_COLUMNS = [
    "quotation_id",
    "reference",
    "extract_reference",
    "bible_id",
    "start_book",
    "start_chapter",
    "start_verse",
    "end_book",
    "end_chapter",
    "end_verse",
    "words",
    "sequence_type",
    "coordinates_from",
    "quotation_types",
    "authors",
    "quoted_author",
    "content",
    "description",
    "verification_status",
    "verification_authority",
    "source",
    "apparatus_letter",
    "sequence_id",
    "extract_id",
]
# Filled from a sequence or a parsed reference (see BookExporter._coordinates).
COORDINATE_COLUMNS = [
    "reference",
    "start_book",
    "start_chapter",
    "start_verse",
    "end_book",
    "end_chapter",
    "end_verse",
    "words",
    "sequence_type",
    "coordinates_from",
    "sequence_id",
]
# Position of the quotation inside the quoting work (patristic mention fields).
POSITION_FIELDS = ["Book", "Chapter", "Paragraph", "Page", "SubPage", "Line"]
WORK_COLUMNS = [
    "work_id",
    "work_title",
    "work_latin_title",
    "work_abbreviation",
    "edition",
    "edition_name",
    "position",
    *(f"pos_{side}_{f.lower()}" for side in ("start", "end") for f in POSITION_FIELDS),
]


@dataclass
class ExportResult:
    rows: list[dict[str, Any]]
    quotations: int
    unresolved_works: int = 0


@dataclass
class _Progress:
    label: str
    every: int = 25
    started: float = field(default_factory=time.monotonic)

    def __call__(self, done: int, total: int | None) -> None:
        if done != 1 and done % self.every and done != total:
            return
        elapsed = time.monotonic() - self.started
        if total:
            eta = elapsed / done * (total - done) if done else 0
            log.info("%s: %d/%d (ETA %s)", self.label, done, total, _fmt_duration(eta))
        else:
            log.info("%s: %d", self.label, done)


class BookExporter:
    def __init__(self, client: BiblIndexClient, bible_id: int) -> None:
        self.client = client
        self.bible_id = bible_id
        self._names: dict[str, str] = {}
        # Flips to False on the first 403 from /api/sequences.
        self.sequences_readable = True

    # -- Core export ------------------------------------------------------------

    def export(
        self,
        book: Book,
        *,
        with_works: bool = False,
        limit: int | None = None,
    ) -> ExportResult:
        flt = {"bible": self.bible_id, "startBook": book.id}

        quotations = list(
            self.client.iter_collection(
                "/api/quotations",
                flt,
                limit=limit,
                on_page=_page_progress("Quotations", 100, limit=limit),
            )
        )
        log.info("%s (bible %s): %d quotations.", book.name, self.bible_id, len(quotations))

        # A full export lists all extracts and sequences of the book (cheap: 100
        # per request). A --limit trial run fetches only the ones it needs.
        lazy = limit is not None
        extracts: dict[str, dict[str, Any]] = {}
        sequences: dict[str, dict[str, Any]] = {}
        if not lazy:
            extracts = self._index(
                self.client.iter_collection(
                    "/api/extracts", flt, on_page=_page_progress("Extracts", 100)
                )
            )
            try:
                sequences = self._index(
                    self.client.iter_collection(
                        "/api/sequences", flt, on_page=_page_progress("Sequences", 100)
                    )
                )
            except PermissionDeniedError:
                self._sequences_denied()
        quotation_types = self._quotation_type_names()
        self._prefetch_authors(quotations)
        works = self.resolve_works(quotations) if with_works else {}

        rows: list[dict[str, Any]] = []
        for quotation in quotations:
            extract = self._lookup(quotation.get("extract"), extracts)
            if lazy:
                self._fetch_sequences(extract, sequences)
            for coordinates in self._coordinates(extract, sequences, book):
                row = self._row(quotation, extract, coordinates, quotation_types)
                if with_works:
                    row.update(dict.fromkeys(WORK_COLUMNS, ""))
                    row.update(works.get(_iri(quotation), {}))
                rows.append(row)

        unresolved = sum(1 for q in quotations if _iri(q) not in works) if with_works else 0
        return ExportResult(
            rows=rows,
            quotations=len(quotations),
            unresolved_works=unresolved,
        )

    # -- Verse coordinates ----------------------------------------------------

    def _coordinates(
        self, extract: dict[str, Any], sequences: dict[str, dict[str, Any]], book: Book
    ) -> list[dict[str, Any]]:
        """Coordinate columns for each span of the extract inside ``book``.

        Prefers the extract's sequences; falls back to parsing its reference
        text; as a last resort returns one row with only the raw reference.
        """
        in_book = [
            sequences[iri]
            for iri in extract.get("sequences") or []
            if iri in sequences and self._in_scope(sequences[iri], book)
        ]
        if in_book:
            return [self._from_sequence(s) for s in in_book]
        reference = extract.get("abbreviation") or ""
        spans = [
            span
            for span in parse_reference(reference)
            if span.parsed and same_book(span.book, book.abbreviation)
        ]
        if spans:
            return [_from_span(s) for s in spans]
        if reference:
            log.debug("Could not parse coordinates from %r", reference)
        return [{"reference": _strip_bible_prefix(reference), "coordinates_from": ""}]

    def _from_sequence(self, sequence: dict[str, Any]) -> dict[str, Any]:
        return {
            "reference": sequence.get("name") or "",
            "start_book": self._book_label(sequence.get("startBook")),
            "start_chapter": sequence.get("startChapter") or "",
            "start_verse": _verse(sequence.get("startVerse"), sequence.get("startLetter")),
            "end_book": self._book_label(sequence.get("endBook")),
            "end_chapter": sequence.get("endChapter") or "",
            "end_verse": _verse(sequence.get("endVerse"), sequence.get("endLetter")),
            "words": _word_range(sequence.get("startWord"), sequence.get("endWord")),
            "sequence_type": sequence.get("type") or "",
            "sequence_id": sequence.get("id", ""),
            "coordinates_from": FROM_SEQUENCE,
        }

    def _fetch_sequences(
        self, extract: dict[str, Any], sequences: dict[str, dict[str, Any]]
    ) -> None:
        """--limit mode: fetch the extract's sequences one by one, if allowed."""
        for iri in extract.get("sequences") or []:
            if not self.sequences_readable:
                return
            try:
                self._lookup(iri, sequences)
            except PermissionDeniedError:
                self._sequences_denied()

    def _sequences_denied(self) -> None:
        if self.sequences_readable:
            log.warning(
                "Your account may not read /api/sequences (403), so verse coordinates are "
                "parsed from each extract's reference text instead (coordinates_from="
                "'reference'). Ask BiblIndex to grant sequence access for exact coordinates."
            )
        self.sequences_readable = False

    def _row(
        self,
        quotation: dict[str, Any],
        extract: dict[str, Any],
        coordinates: dict[str, Any],
        quotation_types: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "quotation_id": quotation.get("id", _id_from_iri(_iri(quotation))),
            "extract_reference": extract.get("abbreviation") or "",
            "bible_id": self.bible_id,
            **dict.fromkeys(COORDINATE_COLUMNS, ""),
            **coordinates,
            "quotation_types": "; ".join(
                quotation_types.get(iri, iri) for iri in quotation.get("quotationTypes") or []
            ),
            "authors": "; ".join(self._author_name(iri) for iri in quotation.get("authors") or []),
            "quoted_author": self._author_name(quotation.get("quotedAuthor")),
            "content": _clean(quotation.get("content")),
            "description": _clean(quotation.get("description")),
            "verification_status": quotation.get("verificationStatus") or "",
            "verification_authority": quotation.get("verificationAuthority") or "",
            "source": quotation.get("source") or "",
            "apparatus_letter": quotation.get("apparatusLetter") or "",
            "extract_id": extract.get("id", ""),
        }

    def _lookup(self, iri: str | None, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Return a resource from ``index``, fetching (and remembering) it if missing."""
        if not iri:
            return {}
        if iri not in index:
            index[iri] = self.client.get(iri)
        return index[iri]

    def _in_scope(self, sequence: dict[str, Any], book: Book) -> bool:
        """Sequence starts in the exported book, in the exported Bible."""
        bible = sequence.get("bible")
        return sequence.get("startBook") == book.iri and (
            not bible or _id_from_iri(bible) == str(self.bible_id)
        )

    @staticmethod
    def _index(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {_iri(item): item for item in items}

    # -- Names of linked resources -------------------------------------------

    def _quotation_type_names(self) -> dict[str, str]:
        try:
            return {
                _iri(item): item.get("name") or item.get("abbreviation") or _iri(item)
                for item in self.client.iter_collection("/api/quotation_types")
            }
        except ApiError as exc:
            log.warning("Could not load quotation type names (%s); writing IRIs.", exc)
            return {}

    def _author_name(self, iri: str | None) -> str:
        return self._name(iri, ("name", "latinName", "abbreviation"))

    def _book_label(self, iri: str | None) -> str:
        return self._name(iri, ("abbreviation", "name"))

    def _name(self, iri: str | None, fields: tuple[str, ...]) -> str:
        """Resolve a public resource IRI to a display name (cached)."""
        if not iri:
            return ""
        if iri not in self._names:
            try:
                data = self.client.get(iri, auth=False)
                self._names[iri] = next((data[f] for f in fields if data.get(f)), iri)
            except ApiError as exc:
                log.warning("Could not resolve %s: %s", iri, exc)
                self._names[iri] = iri
        return self._names[iri]

    # -- Bulk resolution ------------------------------------------------------

    def resolve_all(
        self, iris: Iterable[str | None], collection: str, *, auth: bool, label: str
    ) -> dict[str, dict[str, Any]]:
        """Fetch resources by IRI, listing the whole collection when that is cheaper.

        One-by-one costs a request per uncached IRI; listing costs one request per
        page of ``collection`` (and is cached, so later books reuse it). Anything
        the listing missed (e.g. rows inserted mid-run) is fetched individually.
        """
        wanted = {iri for iri in iris if iri}
        found: dict[str, dict[str, Any]] = {}
        missing = sum(1 for iri in wanted if not self.client.is_cached(iri))
        if missing:
            try:
                found = self._list_matching(collection, wanted, missing, auth=auth, label=label)
            except ApiError as exc:
                log.warning(
                    "%s: could not list %s (%s); fetching one by one.", label, collection, exc
                )
        rest = sorted(wanted - found.keys())
        progress = _Progress(label, every=50)
        for done, iri in enumerate(rest, start=1):
            try:
                found[iri] = self.client.get(iri, auth=auth)
            except ApiError as exc:
                log.warning("Could not fetch %s: %s", iri, exc)
            if missing:
                progress(done, len(rest))
        return found

    def _list_matching(
        self, collection: str, wanted: set[str], missing: int, *, auth: bool, label: str
    ) -> dict[str, dict[str, Any]]:
        """List ``collection`` if that takes fewer requests than ``missing`` singles."""
        pages = -(-self.client.total_items(collection, auth=auth) // PAGE_SIZE)
        if missing <= pages:
            return {}
        log.info("%s: listing all %d pages instead of %d single requests.", label, pages, missing)
        found: dict[str, dict[str, Any]] = {}
        for item in self.client.iter_collection(
            collection,
            auth=auth,
            items_per_page=PAGE_SIZE,
            on_page=_page_progress(label, PAGE_SIZE),
        ):
            if _iri(item) in wanted:
                found[_iri(item)] = item
        return found

    def _prefetch_authors(self, quotations: list[dict[str, Any]]) -> None:
        iris = [iri for q in quotations for iri in (q.get("authors") or [])]
        iris += [q.get("quotedAuthor") for q in quotations]
        iris = [iri for iri in iris if iri and iri not in self._names]
        for iri, author in self.resolve_all(
            iris, "/api/authors", auth=False, label="Authors"
        ).items():
            self._names[iri] = (
                author.get("name") or author.get("latinName") or author.get("abbreviation") or iri
            )

    # -- Works and positions (--with-works) -----------------------------------

    def resolve_works(self, quotations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Map quotation IRI -> work, edition and position columns.

        Chain: quotation -> patristic mention (position; needs the API-client
        role) -> work edition -> work (title) and edition (e.g. PG48).
        """
        mentions = self.resolve_all(
            (q.get("patristicMention") for q in quotations),
            "/api/patristic_mentions",
            auth=True,
            label="Patristic mentions",
        )
        work_editions = self.resolve_all(
            (m.get("workEdition") for m in mentions.values()),
            "/api/work_editions",
            auth=False,
            label="Work editions",
        )
        works = self.resolve_all(
            (we.get("work") for we in work_editions.values()),
            "/api/works",
            auth=False,
            label="Works",
        )
        editions = self.resolve_all(
            (we.get("edition") for we in work_editions.values()),
            "/api/editions",
            auth=False,
            label="Editions",
        )
        result: dict[str, dict[str, Any]] = {}
        for quotation in quotations:
            mention = mentions.get(quotation.get("patristicMention") or "")
            if mention is None:
                continue
            work_edition = work_editions.get(mention.get("workEdition") or "", {})
            work = works.get(work_edition.get("work") or "", {})
            edition = editions.get(work_edition.get("edition") or "", {})
            result[_iri(quotation)] = work_columns(mention, work, edition)
        return result


def work_columns(
    mention: dict[str, Any], work: dict[str, Any], edition: dict[str, Any]
) -> dict[str, Any]:
    abbreviation = work.get("abbreviation") or ""
    columns = {
        "work_id": work.get("id", _id_from_iri(_iri(work))),
        "work_title": work.get("name") or "",
        "work_latin_title": work.get("latinName") or "",
        "work_abbreviation": abbreviation,
        "edition": edition.get("abbreviation") or "",
        "edition_name": edition.get("name") or "",
        "position": format_position(mention, abbreviation, _page_unit(edition)),
    }
    for side in ("start", "end"):
        for field_name in POSITION_FIELDS:
            value = mention.get(f"{side}{field_name}")
            columns[f"pos_{side}_{field_name.lower()}"] = "" if value is None else value
    return columns


def format_position(mention: dict[str, Any], work_abbreviation: str, page_unit: str) -> str:
    """Format a mention like the website: ``[IVD] 1 (col.77, l.38) - 1 (col.77, l.38)``."""
    start = _position_point(mention, "start", page_unit)
    end = _position_point(mention, "end", page_unit)
    text = f"{start} - {end}" if start and end else start or end
    if work_abbreviation and text:
        text = f"[{work_abbreviation}] {text}"
    return text


def _position_point(mention: dict[str, Any], side: str, page_unit: str) -> str:
    # Divisions of the work (book, chapter, paragraph); the website shows the
    # separator only for paragraph-only works, so "." is our convention.
    division = ".".join(
        str(mention[f"{side}{key}"])
        for key in ("Book", "Chapter", "Paragraph")
        if mention.get(f"{side}{key}") not in (None, "")
    )
    place = []
    if mention.get(f"{side}Page") not in (None, ""):
        place.append(f"{page_unit}{mention[f'{side}Page']}{mention.get(f'{side}SubPage') or ''}")
    if mention.get(f"{side}Line") not in (None, ""):
        place.append(f"l.{mention[f'{side}Line']}")
    if place:
        return f"{division} ({', '.join(place)})".strip()
    return division


def _page_unit(edition: dict[str, Any]) -> str:
    """Patrologia Graeca/Latina are numbered in columns, everything else in pages."""
    collection = (edition.get("collection") or "").casefold()
    abbreviation = edition.get("abbreviation") or ""
    if collection.startswith(("patrologia graeca", "patrologia latina")) or re.match(
        r"^P[GL]\s?\d", abbreviation
    ):
        return "col."
    return "p."


# -- CSV ----------------------------------------------------------------------


def write_csv(rows: list[dict[str, Any]], path: Path, *, with_works: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = BASE_COLUMNS + (WORK_COLUMNS if with_works else [])
    # utf-8-sig so Excel shows Greek/Latin text correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# -- Helpers ------------------------------------------------------------------


def _iri(item: dict[str, Any]) -> str:
    return item.get("@id") or ""


def _id_from_iri(iri: str) -> str:
    return iri.rstrip("/").rsplit("/", 1)[-1] if iri else ""


def _from_span(span: Span) -> dict[str, Any]:
    return {
        "reference": f"{span.book} {span.text.removeprefix(span.book).strip()}".strip(),
        "start_book": span.book,
        "start_chapter": span.start_chapter,
        "start_verse": span.start_verse,
        "end_book": span.book,
        "end_chapter": span.end_chapter,
        "end_verse": span.end_verse,
        "words": span.words,
        "sequence_type": span.type,
        "coordinates_from": FROM_REFERENCE,
    }


def _strip_bible_prefix(reference: str) -> str:
    if reference.startswith("[") and "]" in reference:
        return reference.split("]", 1)[1].strip()
    return reference


def _word_range(start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return ""
    return f"{start or ''}-{end or ''}" if start != end else str(start)


def _verse(verse: str | None, letter: str | None) -> str:
    if not verse:
        return ""
    return f"{verse}{letter or ''}"


def _clean(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _page_progress(
    label: str, per_page: int, *, limit: int | None = None
) -> Callable[[int, int | None], None]:
    progress = _Progress(label, every=10)

    def on_page(page: int, total_items: int | None) -> None:
        if total_items and limit is not None:
            total_items = min(total_items, limit)
        pages = -(-total_items // per_page) if total_items else None
        progress(page, pages)

    return on_page


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
