"""Bible books (public reference data) and resolving a user's book argument."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import BiblIndexClient


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
