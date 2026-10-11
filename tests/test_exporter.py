import csv

import pytest

from biblindex_importer.books import Bible, Book
from biblindex_importer.cache import ResponseCache
from biblindex_importer.exporter import (
    BASE_COLUMNS,
    WORK_COLUMNS,
    BookExporter,
    format_position,
    write_csv,
)

JOHN = Book(50, "/api/books/50", "John", "Jn", "43N", "jn")
JOHN_FILTER = {"bible": 8, "startBook": 50}


def seq(n, book, chapter, verse, end_verse=None, name=""):
    return {
        "@id": f"/api/sequences/{n}",
        "id": n,
        "type": "verse" if end_verse is None else "consecutive_verses",
        "name": name,
        "bible": "/api/bibles/8",
        "startBook": f"/api/books/{book}",
        "startChapter": chapter,
        "startVerse": verse,
        "endBook": f"/api/books/{book}",
        "endChapter": chapter,
        "endVerse": end_verse or verse,
        "_f": {"bible": 8, "startBook": book},
    }


def quotation(n, extract, mention, work, book=50, **extra):
    return {
        "@id": f"/api/quotations/{n}",
        "id": n,
        "extract": f"/api/extracts/{extract}",
        "patristicMention": f"/api/patristic_mentions/{mention}",
        "authors": ["/api/authors/90"],
        "quotedAuthor": None,
        "quotationTypes": ["/api/quotation_types/1"],
        "content": "In  principio\n erat verbum",
        "verificationStatus": "checked",
        "source": "BIBLINDEX-SC",
        "_f": {"bible": 8, "startBook": book, "work": work},
        **extra,
    }


@pytest.fixture
def biblindex(api):
    api.add_collection(
        "/api/books",
        [
            {"@id": "/api/books/47", "id": 47, "abbreviation": "Mt", "name": "Matthew"},
            {"@id": "/api/books/50", "id": 50, "abbreviation": "Jn", "name": "John"},
        ],
    )
    api.add_collection(
        "/api/sequences",
        [
            seq(1, 50, "1", "1", name="Jn 1:1"),
            seq(2, 50, "3", "16", "18", name="Jn 3:16-18"),
            seq(3, 47, "5", "3", name="Mt 5:3"),
        ],
    )
    api.add_collection(
        "/api/extracts",
        [
            {"@id": "/api/extracts/10", "id": 10, "abbreviation": "Jn 1:1",
             "sequences": ["/api/sequences/1"], "_f": {"bible": 8, "startBook": 50}},
            {"@id": "/api/extracts/11", "id": 11, "abbreviation": "Jn 3:16-18; Mt 5:3",
             "sequences": ["/api/sequences/2", "/api/sequences/3"],
             "_f": {"bible": 8, "startBook": 50}},
            {"@id": "/api/extracts/12", "id": 12, "abbreviation": "Mt 5:3",
             "sequences": ["/api/sequences/3"], "_f": {"bible": 8, "startBook": 47}},
        ],
    )
    api.add_collection(
        "/api/quotations",
        [
            quotation(100, 10, 500, work=7),
            quotation(101, 11, 501, work=8, quotedAuthor="/api/authors/91"),
            quotation(102, 12, 502, work=8, book=47),
        ],
    )
    api.add_collection(
        "/api/quotation_types",
        [{"@id": "/api/quotation_types/1", "name": "Quotation"}],
    )
    api.add(
        {"@id": "/api/authors/90", "name": "Augustine of Hippo"},
        {"@id": "/api/authors/91", "name": "Ambrose"},
    )
    api.add_collection(
        "/api/patristic_mentions",
        [
            # Same shape as the live mention behind quotation 1321591.
            {"@id": "/api/patristic_mentions/500", "id": 500,
             "workEdition": "/api/work_editions/70", "startParagraph": "1",
             "startPage": "77", "startLine": 38, "endParagraph": "1", "endPage": "77",
             "endLine": 38},
            {"@id": "/api/patristic_mentions/501", "id": 501,
             "workEdition": "/api/work_editions/71", "startChapter": "4",
             "startPage": "12"},
            {"@id": "/api/patristic_mentions/502", "id": 502,
             "workEdition": "/api/work_editions/71"},
        ],
    )
    api.add_collection(
        "/api/work_editions",
        [
            {"@id": "/api/work_editions/70", "work": "/api/works/7",
             "edition": "/api/editions/608"},
            {"@id": "/api/work_editions/71", "work": "/api/works/8",
             "edition": "/api/editions/9"},
        ],
    )
    api.add_collection(
        "/api/editions",
        [
            {"@id": "/api/editions/608", "abbreviation": "PG48",
             "name": "Patrologia Graeca 48", "collection": "Patrologia Graeca"},
            {"@id": "/api/editions/9", "abbreviation": "CCSL27",
             "name": "Corpus Christianorum 27", "collection": "CCSL"},
        ],
    )
    api.add_collection(
        "/api/works",
        [
            {"@id": "/api/works/7", "id": 7, "name": "Contra Iudaeos, gentiles et haereticos",
             "latinName": "Contra Iudaeos, gentiles et haereticos", "abbreviation": "IVD"},
            {"@id": "/api/works/8", "id": 8, "name": "Confessions", "latinName": "Confessiones"},
            {"@id": "/api/works/9", "id": 9, "name": "Unrelated"},
        ],
    )
    return api


def test_export_joins_quotations_with_verse_coordinates(biblindex, make_client):
    result = BookExporter(make_client(), bibles=[8]).export(JOHN)

    assert result.quotations == 2  # the Matthew-only quotation is filtered out
    assert [r["quotation_id"] for r in result.rows] == [100, 101]
    first, second = result.rows
    assert first["reference"] == "Jn 1:1"
    assert (first["start_book"], first["start_chapter"], first["start_verse"]) == ("Jn", "1", "1")
    assert first["authors"] == "Augustine of Hippo"
    assert first["quotation_types"] == "Quotation"
    assert first["content"] == "In principio erat verbum"
    assert first["bible_id"] == 8
    # Extract spanning John and Matthew: only the John sequence becomes a row.
    assert second["reference"] == "Jn 3:16-18"
    assert second["extract_reference"] == "Jn 3:16-18; Mt 5:3"
    assert (second["end_chapter"], second["end_verse"]) == ("3", "18")
    assert second["quoted_author"] == "Ambrose"


def test_export_uses_book_filters_on_every_listing(biblindex, make_client):
    BookExporter(make_client(), bibles=[8]).export(JOHN)
    for path in ("/api/quotations", "/api/extracts", "/api/sequences"):
        params = biblindex.calls_to(path)[0][1]
        assert (params["bible"], params["startBook"]) == ("8", "50")


def test_export_one_row_per_john_sequence(biblindex, make_client):
    biblindex.resources["/api/extracts/10"]["sequences"] = [
        "/api/sequences/1",
        "/api/sequences/2",
    ]
    result = BookExporter(make_client(), bibles=[8]).export(JOHN)
    assert [r["reference"] for r in result.rows] == ["Jn 1:1", "Jn 3:16-18", "Jn 3:16-18"]


def test_export_with_limit_fetches_only_what_it_needs(biblindex, make_client):
    result = BookExporter(make_client(), bibles=[8]).export(JOHN, limit=1)
    assert [r["reference"] for r in result.rows] == ["Jn 1:1"]
    assert biblindex.calls_to("/api/extracts") == []
    assert biblindex.calls_to("/api/sequences") == []
    assert len(biblindex.calls_to("/api/extracts/10")) == 1


def test_rows_record_sequence_as_coordinate_source(biblindex, make_client):
    result = BookExporter(make_client(), bibles=[8]).export(JOHN)
    assert {r["coordinates_from"] for r in result.rows} == {"sequence"}
    assert result.rows[0]["sequence_id"] == 1


def deny_sequences(api):
    for path in ["/api/sequences"] + [f"/api/sequences/{n}" for n in (1, 2, 3)]:
        for _ in range(3):
            api.queue(path, 403, '{"detail": "Access Denied."}')


@pytest.mark.parametrize("limit", [None, 2])
def test_denied_sequences_fall_back_to_reference_text(biblindex, make_client, limit, caplog):
    deny_sequences(biblindex)
    exporter = BookExporter(make_client(), bibles=[8])
    result = exporter.export(JOHN, limit=limit)

    assert not exporter.sequences_readable
    assert caplog.text.count("may not read /api/sequences") == 1
    first, second = result.rows
    assert first["coordinates_from"] == "reference"
    assert (first["start_chapter"], first["start_verse"]) == ("1", "1")
    # Extract 11 is "Jn 3:16-18; Mt 5:3": only the John span becomes a row.
    assert second["reference"] == "Jn 3:16-18"
    assert (second["end_chapter"], second["end_verse"]) == ("3", "18")
    assert second["sequence_type"] == "consecutive_verses"
    assert second["sequence_id"] == ""


def test_denied_sequences_are_not_retried_per_quotation(biblindex, make_client):
    deny_sequences(biblindex)
    BookExporter(make_client(), bibles=[8]).export(JOHN, limit=2)
    sequence_calls = [c for c in biblindex.calls if c[0].startswith("/api/sequences")]
    assert len(sequence_calls) == 1


def test_unparseable_reference_keeps_raw_text(biblindex, make_client):
    deny_sequences(biblindex)
    biblindex.resources["/api/extracts/10"]["abbreviation"] = "[JB] Jn prologue"
    result = BookExporter(make_client(), bibles=[8]).export(JOHN)
    first = result.rows[0]
    assert first["reference"] == "Jn prologue"
    assert first["coordinates_from"] == ""
    assert first["start_chapter"] == ""


def test_authors_are_resolved_once_and_anonymously(biblindex, make_client):
    BookExporter(make_client(), bibles=[8]).export(JOHN)
    calls = biblindex.calls_to("/api/authors/90")
    assert len(calls) == 1
    assert "Authorization" not in calls[0][2]


def test_with_works_adds_work_edition_and_position(biblindex, make_client):
    result = BookExporter(make_client(), bibles=[8]).export(JOHN, with_works=True)
    first, second = result.rows
    # Matches the website: "[IVD] 1 (col.77, l.38) - 1 (col.77, l.38)" in PG48.
    assert first["work_title"] == "Contra Iudaeos, gentiles et haereticos"
    assert first["work_abbreviation"] == "IVD"
    assert (first["edition"], first["edition_name"]) == ("PG48", "Patrologia Graeca 48")
    assert first["position"] == "[IVD] 1 (col.77, l.38) - 1 (col.77, l.38)"
    assert (first["pos_start_paragraph"], first["pos_start_page"], first["pos_start_line"]) == (
        "1",
        "77",
        38,
    )
    assert second["work_title"] == "Confessions"
    assert second["position"] == "4 (p.12)"  # not a patrology: pages, not columns
    assert result.unresolved_works == 0
    assert set(WORK_COLUMNS) <= set(first)


def test_with_works_fetches_few_mentions_one_by_one(biblindex, make_client):
    # 1 mention needed vs a 1-page collection: a single request is not worse.
    BookExporter(make_client(), bibles=[8]).export(JOHN, with_works=True, limit=1)
    listed = [
        c for c in biblindex.calls_to("/api/patristic_mentions") if c[1]["itemsPerPage"] != "1"
    ]
    assert listed == []
    assert len(biblindex.calls_to("/api/patristic_mentions/500")) == 1


def test_with_works_lists_collection_when_cheaper(biblindex, make_client):
    # 2 mentions needed, the collection fits on 1 page: listing is cheaper.
    exporter = BookExporter(make_client(), bibles=[8])
    quotations = [biblindex.resources[f"/api/quotations/{n}"] for n in (100, 101)]
    works = exporter.resolve_works(quotations)
    assert works["/api/quotations/100"]["work_abbreviation"] == "IVD"
    assert biblindex.calls_to("/api/patristic_mentions/500") == []
    pages = [
        c for c in biblindex.calls_to("/api/patristic_mentions") if c[1]["itemsPerPage"] == "100"
    ]
    assert len(pages) == 1


def test_resolve_all_fetches_what_the_listing_missed(biblindex, make_client):
    exporter = BookExporter(make_client(), bibles=[8])
    biblindex.add({"@id": "/api/patristic_mentions/999", "workEdition": None})  # not listed
    found = exporter.resolve_all(
        ["/api/patristic_mentions/500", "/api/patristic_mentions/501",
         "/api/patristic_mentions/999"],
        "/api/patristic_mentions",
        auth=True,
        label="Mentions",
    )
    assert set(found) == {f"/api/patristic_mentions/{n}" for n in (500, 501, 999)}
    assert len(biblindex.calls_to("/api/patristic_mentions/999")) == 1


def test_resolve_all_falls_back_when_listing_fails(biblindex, make_client):
    exporter = BookExporter(make_client(), bibles=[8])
    found = exporter.resolve_all(
        ["/api/authors/90", "/api/authors/91"], "/api/authors", auth=False, label="Authors"
    )
    assert found["/api/authors/90"]["name"] == "Augustine of Hippo"


def test_missing_mention_counts_as_unresolved(biblindex, make_client):
    del biblindex.resources["/api/patristic_mentions/501"]
    biblindex.collections["/api/patristic_mentions"].pop(1)
    result = BookExporter(make_client(), bibles=[8]).export(JOHN, with_works=True)
    assert result.unresolved_works == 1
    assert result.rows[1]["position"] == ""


@pytest.mark.parametrize(
    ("mention", "abbreviation", "unit", "expected"),
    [
        ({"startBook": "2", "startChapter": "3", "startParagraph": "4"}, "Haer", "p.",
         "[Haer] 2.3.4"),
        ({"startPage": "77", "startSubPage": "B", "startLine": 5}, "", "col.", "(col.77B, l.5)"),
        ({}, "IVD", "p.", ""),
        ({"startChapter": "1", "endChapter": "2"}, "", "p.", "1 - 2"),
    ],
)
def test_format_position(mention, abbreviation, unit, expected):
    assert format_position(mention, abbreviation, unit) == expected


def test_rerun_with_cache_makes_no_rate_limited_requests(biblindex, make_client):
    cache = ResponseCache(":memory:")
    BookExporter(make_client(cache=cache), bibles=[8]).export(JOHN, with_works=True)
    calls_before = len(biblindex.calls)
    client = make_client(cache=cache)
    BookExporter(client, bibles=[8]).export(JOHN, with_works=True)
    assert len(biblindex.calls) == calls_before
    assert client.auth_limiter.stats.requests == 0


def test_write_csv_round_trip(tmp_path):
    path = tmp_path / "out" / "john.csv"
    rows = [{"quotation_id": 1, "reference": "Jn 1:1", "content": "Ἐν ἀρχῇ ἦν ὁ λόγος"}]
    write_csv(rows, path, with_works=True)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM so Excel detects UTF-8
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == BASE_COLUMNS + WORK_COLUMNS
        (row,) = list(reader)
    assert row["content"] == "Ἐν ἀρχῇ ἦν ὁ λόγος"
    assert row["work_title"] == ""


def test_write_csv_without_works_has_no_work_columns(tmp_path):
    path = tmp_path / "john.csv"
    write_csv([], path, with_works=False)
    with path.open(encoding="utf-8-sig") as fh:
        header = fh.readline().strip().split(",")
    assert header == BASE_COLUMNS


# -- Several Bibles -------------------------------------------------------------


def add_vulgate_quotation(api, n=110, also_in_jb=None):
    """A John quotation indexed in the Vulgate (bible 2) instead of JB (bible 8)."""
    api.collections["/api/sequences"].append(
        {**seq(4, 50, "3", "16", name="Jn 3:16"), "bible": "/api/bibles/2",
         "_f": {"bible": 2, "startBook": 50}}
    )
    api.add({**api.collections["/api/sequences"][-1]})
    extract = {"@id": "/api/extracts/20", "id": 20, "abbreviation": "[Vg] Jn 3:16",
               "sequences": ["/api/sequences/4"], "_f": {"bible": 2, "startBook": 50}}
    api.collections["/api/extracts"].append(extract)
    api.add(extract)
    vg = quotation(n, 20, 500, work=7)
    vg["_f"]["bible"] = 2
    api.collections["/api/quotations"].append(vg)
    api.add(vg)


def test_export_covers_several_bibles(biblindex, make_client):
    add_vulgate_quotation(biblindex)
    bibles = [Bible(8, "JB"), Bible(2, "Vg")]
    result = BookExporter(make_client(), bibles).export(JOHN)

    assert [(r["quotation_id"], r["bible"], r["bible_id"]) for r in result.rows] == [
        (100, "JB", 8),
        (101, "JB", 8),
        (110, "Vg", 2),
    ]
    assert result.per_bible == {"JB": 2, "Vg": 1}
    vg_row = result.rows[2]
    assert (vg_row["reference"], vg_row["coordinates_from"]) == ("Jn 3:16", "sequence")
    queried = {c[1]["bible"] for c in biblindex.calls_to("/api/quotations")}
    assert queried == {"8", "2"}


def test_quotation_indexed_in_two_bibles_is_exported_once(biblindex, make_client):
    twin = {**biblindex.resources["/api/quotations/100"]}
    twin["_f"] = {**twin["_f"], "bible": 2}
    biblindex.collections["/api/quotations"].append(twin)
    result = BookExporter(make_client(), [8, 2]).export(JOHN)
    assert [r["quotation_id"] for r in result.rows] == [100, 101]


def test_limit_spans_bibles(biblindex, make_client):
    add_vulgate_quotation(biblindex)
    result = BookExporter(make_client(), [8, 2]).export(JOHN, limit=3)
    assert [r["quotation_id"] for r in result.rows] == [100, 101, 110]
    result = BookExporter(make_client(), [8, 2]).export(JOHN, limit=2)
    assert [r["quotation_id"] for r in result.rows] == [100, 101]


def test_bibles_without_quotations_are_skipped_cheaply(biblindex, make_client):
    result = BookExporter(make_client(), [8, 5]).export(JOHN)
    assert result.per_bible == {"8": 2}
    assert [c for c in biblindex.calls_to("/api/extracts") if c[1].get("bible") == "5"] == []


def test_write_csv_keeps_one_record_per_line(tmp_path):
    path = tmp_path / "john.csv"
    rows = [
        {"quotation_id": 1, "edition_name": "ASSEMANI J.S., Ephraem Syri opera\r\n"},
        {"quotation_id": 2, "edition_name": "Two\nlines  and\ttabs", "position": None},
    ]
    write_csv(rows, path, with_works=True)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 1 + len(rows)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        parsed = list(csv.DictReader(fh))
    assert [r["edition_name"] for r in parsed] == [
        "ASSEMANI J.S., Ephraem Syri opera",
        "Two lines and tabs",
    ]
