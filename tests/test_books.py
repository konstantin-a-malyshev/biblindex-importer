import pytest

from biblindex_importer.books import (
    Bible,
    Book,
    BookNotFoundError,
    fetch_books,
    find_book,
    select_bibles,
)

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


@pytest.fixture
def bibles_api(api):
    api.add_collection(
        "/api/bibles",
        [{"@id": f"/api/bibles/{i}", "id": i, "abbreviation": a, "name": n}
         for i, a, n in [(2, "Vg", "Vulgate"), (4, "NTG", "NT Graecum"), (8, "JB", "BJ")]],
    )
    return api


def test_select_all_bibles(bibles_api, make_client):
    assert [b.label for b in select_bibles(make_client(), "all")] == ["Vg", "NTG", "JB"]
    assert [b.label for b in select_bibles(make_client(), " ALL ")] == ["Vg", "NTG", "JB"]


def test_select_bible_ids_keep_order_and_labels(bibles_api, make_client):
    assert select_bibles(make_client(), "8, 2") == [
        Bible(8, "JB", "BJ"),
        Bible(2, "Vg", "Vulgate"),
    ]


def test_unknown_or_unlisted_bible_ids_still_work(api, make_client):
    # /api/bibles unavailable (404 here): explicit ids are used without labels.
    assert select_bibles(make_client(), "8") == [Bible(8)]


def test_invalid_bible_spec(bibles_api, make_client):
    with pytest.raises(ValueError, match="all"):
        select_bibles(make_client(), "JB")
