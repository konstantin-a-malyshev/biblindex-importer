"""Parse BiblIndex reference strings such as ``[JB] Jn 1:1 w1-16``.

Extracts carry a compact reference (``abbreviation``) derived from their
sequences. When an account may not read ``/api/sequences``, this text is the
only source of chapter/verse coordinates. Observed shapes (bible 8, John):

    [JB] Jn 17:21          one verse
    [JB] Jn 16:27-28       verse range in one chapter
    [JB] Jn 1:1 w1-16      words 1-16 of one verse
    [JB] Jn 4              whole chapter
    [JB] Jn                whole book

The parser also accepts cross-chapter ranges (``Jn 1:1-2:3``), verse letters
(``3:16a``) and several spans separated by ``;``. A span it cannot read is
returned with ``parsed=False`` so the caller can keep the raw text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BIBLE_PREFIX = re.compile(r"^\s*\[(?P<bible>[^\]]*)\]\s*")
_BOOK = r"(?:[1-4]\s?)?[A-Za-z][A-Za-z.]*(?:\s\([^)]*\))?"
# A reference to a whole book, e.g. "[JB] Jn" (150 of John's JB quotations).
_BOOK_ONLY = re.compile(rf"^(?P<book>{_BOOK})\s*$")
# "\s*" after the colon: 127 Vulgate references read "[Vg] Jn 14: 23".
_POINT = r"\d+[A-Za-z]?(?::\s*\d+[a-z]?)?"
_SPAN = re.compile(
    rf"^(?:(?P<book>{_BOOK})\s+)?"
    rf"(?P<start>{_POINT})"
    # The (?![\d:]) keeps "w3-1:2 w5" from reading "1" as the last word.
    r"(?:\s*w(?P<start_word>\d+)(?:-(?P<start_word_end>\d+)(?![\d:]))?)?"
    rf"(?:\s*-\s*(?P<end>{_POINT})(?:\s*w(?P<end_word>\d+))?)?\s*$"
)


@dataclass(frozen=True)
class Span:
    text: str
    bible: str = ""
    book: str = ""
    start_chapter: str = ""
    start_verse: str = ""
    end_chapter: str = ""
    end_verse: str = ""
    words: str = ""
    parsed: bool = True

    @property
    def type(self) -> str:
        """Same vocabulary as the API's sequence ``type``."""
        if not self.parsed:
            return ""
        if not self.start_chapter:
            return "book"
        if not self.start_verse:
            return "chapter" if self.start_chapter == self.end_chapter else "consecutive_chapters"
        if (self.start_chapter, self.start_verse) == (self.end_chapter, self.end_verse):
            return "verse"
        return "consecutive_verses"


def parse_reference(text: str | None) -> list[Span]:
    """Split a reference into spans; a span without a book inherits the previous one."""
    if not text or not text.strip():
        return []
    spans: list[Span] = []
    bible = ""
    book = ""
    for raw in text.split(";"):
        raw = raw.strip()
        if not raw:
            continue
        prefix = _BIBLE_PREFIX.match(raw)
        if prefix:
            bible = prefix.group("bible").strip()
            raw = raw[prefix.end() :]
        whole_book = _BOOK_ONLY.match(raw)
        if whole_book:
            book = whole_book.group("book").strip()
            spans.append(Span(text=raw, bible=bible, book=book))
            continue
        match = _SPAN.match(raw)
        if not match:
            spans.append(Span(text=raw, bible=bible, book=book, parsed=False))
            continue
        book = (match.group("book") or book).strip()
        start_chapter, start_verse = _split_point(match.group("start"))
        end_chapter, end_verse = start_chapter, start_verse
        if match.group("end"):
            end_chapter, end_verse = _split_point(match.group("end"))
            if ":" not in match.group("end") and start_verse:
                # "16:27-28": the end is a verse in the same chapter.
                end_chapter, end_verse = start_chapter, match.group("end")
        spans.append(
            Span(
                text=raw,
                bible=bible,
                book=book,
                start_chapter=start_chapter,
                start_verse=start_verse,
                end_chapter=end_chapter,
                end_verse=end_verse,
                words=_words(match),
            )
        )
    return spans


def same_book(a: str, b: str) -> bool:
    """Compare book abbreviations, ignoring case and spaces ("1 Jn" == "1Jn")."""
    return a.replace(" ", "").casefold() == b.replace(" ", "").casefold()


def _split_point(point: str) -> tuple[str, str]:
    chapter, _, verse = point.partition(":")
    return chapter, verse.strip()


def _words(match: re.Match[str]) -> str:
    start, start_end, end = (
        match.group("start_word"),
        match.group("start_word_end"),
        match.group("end_word"),
    )
    if start and end:
        return f"{start}-{end}"
    if start and start_end:
        return f"{start}-{start_end}"
    return start or ""
