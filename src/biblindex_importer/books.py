"""Bible books and Bible editions (public reference data), and resolving the
user's ``--book`` and ``--bible`` arguments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import ApiError, BiblIndexClient

ALL_BIBLES = "all"


class BookNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class Book:
    id: int
    iri: str
    name: str
    abbreviation: str
    code: str
    slug: str

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Book:
        return cls(
            id=int(item["id"]),
            iri=item.get("@id") or f"/api/books/{item['id']}",
            name=item.get("name") or "",
            abbreviation=item.get("abbreviation") or "",
            code=item.get("code") or "",
            slug=item.get("slug") or "",
        )


def fetch_books(client: BiblIndexClient) -> list[Book]:
    return [Book.from_api(item) for item in client.iter_collection("/api/books", auth=False)]


def find_book(books: list[Book], query: str) -> Book:
    """Match by id, name, abbreviation, code or slug (case-insensitive).

    Accepts e.g. "50", "John", "Jn", "43N" or "jn".
    """
    wanted = query.strip().casefold()
    if wanted.isdigit():
        for book in books:
            if book.id == int(wanted):
                return book
    for field in ("name", "abbreviation", "code", "slug"):
        matches = [b for b in books if getattr(b, field).casefold() == wanted]
        if len(matches) == 1:
            return matches[0]
    suggestions = [b.name for b in books if wanted in b.name.casefold()][:10]
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise BookNotFoundError(f"No Bible book matches {query!r}.{hint}")


# -- Bible editions -------------------------------------------------------------


@dataclass(frozen=True)
class Bible:
    """A Bible edition; verse numbers in BiblIndex follow a specific edition."""

    id: int
    abbreviation: str = ""
    name: str = ""

    @property
    def label(self) -> str:
        return self.abbreviation or str(self.id)


def fetch_bibles(client: BiblIndexClient) -> list[Bible]:
    return [
        Bible(int(item["id"]), item.get("abbreviation") or "", item.get("name") or "")
        for item in client.iter_collection("/api/bibles", auth=False)
    ]


def select_bibles(client: BiblIndexClient, spec: str) -> list[Bible]:
    """Turn ``all`` or a comma-separated id list (``8,4,2``) into Bibles.

    A quotation is indexed against the Bible its author quoted (Greek authors
    against the Greek NT, Latin ones against the Vulgate, ...), so ``all`` is
    needed to get every quotation of a book.
    """
    spec = spec.strip().casefold()
    try:
        known = {bible.id: bible for bible in fetch_bibles(client)}
    except ApiError:
        if spec == ALL_BIBLES:
            raise
        known = {}  # labels are a nicety; explicit ids still work
    if spec == ALL_BIBLES:
        return list(known.values())
    try:
        ids = [int(part) for part in spec.split(",") if part.strip()]
    except ValueError:
        raise ValueError(f"Bible must be 'all' or ids like '8,4,2', not {spec!r}") from None
    if not ids:
        raise ValueError("No Bible id given.")
    return [known.get(bible_id, Bible(bible_id)) for bible_id in ids]
