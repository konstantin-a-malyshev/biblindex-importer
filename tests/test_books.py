import pytest

from biblindex_importer.books import Book, BookNotFoundError, fetch_books, find_book

BOOKS = [
    Book(47, "/api/books/47", "Matthew", "Mt", "40N", "mt"),
    Book(50, "/api/books/50", "John", "Jn", "43N", "jn"),
    Book(103, "/api/books/103", "1 John", "1Jn", "62N", "1jn"),
]


@pytest.mark.parametrize("query", ["John", "john", " JOHN ", "Jn", "43N", "jn", "50"])
def test_find_book_accepts_many_spellings(query):
    assert find_book(BOOKS, query).id == 50


def test_find_book_does_not_confuse_john_and_1_john():
    assert find_book(BOOKS, "1 John").id == 103


def test_unknown_book_suggests_close_names():
    with pytest.raises(BookNotFoundError, match="John, 1 John"):
        find_book(BOOKS, "Joh")


def test_fetch_books_is_public_and_paginated(api, make_client):
    api.add_collection(
        "/api/books",
        [
            {"@id": "/api/books/47", "id": 47, "abbreviation": "Mt", "code": "40N",
             "name": "Matthew", "slug": "mt"},
            {"@id": "/api/books/50", "id": 50, "abbreviation": "Jn", "code": "43N",
             "name": "John", "slug": "jn"},
        ],
    )
    books = fetch_books(make_client())
    assert [b.name for b in books] == ["Matthew", "John"]
    assert books[1].iri == "/api/books/50"
    assert api.token_requests == []
