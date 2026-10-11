import pytest

from biblindex_importer.references import Span, parse_reference, same_book


def coords(span: Span):
    return (span.start_chapter, span.start_verse, span.end_chapter, span.end_verse, span.words)


@pytest.mark.parametrize(
    ("text", "expected", "kind"),
    [
        # Shapes observed in live data for John (bible 8).
        ("[JB] Jn 17:21", ("17", "21", "17", "21", ""), "verse"),
        ("[JB] Jn 16:27-28", ("16", "27", "16", "28", ""), "consecutive_verses"),
        ("[JB] Jn 1:1 w1-16", ("1", "1", "1", "1", "1-16"), "verse"),
        ("[JB] Jn 4", ("4", "", "4", "", ""), "chapter"),
        ("[JB] Jn", ("", "", "", "", ""), "book"),
        # Plausible shapes not yet seen live.
        ("Jn 1:1-2:3", ("1", "1", "2", "3", ""), "consecutive_verses"),
        ("Jn 3:16a", ("3", "16a", "3", "16a", ""), "verse"),
        ("Jn 1:1 w3-1:2 w5", ("1", "1", "1", "2", "3-5"), "consecutive_verses"),
        ("Jn 1-3", ("1", "", "3", "", ""), "consecutive_chapters"),
    ],
)
def test_single_span_shapes(text, expected, kind):
    (span,) = parse_reference(text)
    assert span.parsed
    assert span.book == "Jn"
    assert coords(span) == expected
    assert span.type == kind


def test_bible_prefix_is_separated():
    (span,) = parse_reference("[JB] Jn 17:21")
    assert span.bible == "JB"
    assert span.text == "Jn 17:21"


def test_multiple_spans_and_books():
    spans = parse_reference("[JB] Jn 3:16-18; Mt 5:3")
    assert [(s.book, s.start_chapter, s.start_verse) for s in spans] == [
        ("Jn", "3", "16"),
        ("Mt", "5", "3"),
    ]
    assert spans[1].bible == "JB"  # prefix carries over


def test_span_without_book_inherits_previous_book():
    spans = parse_reference("Jn 3:16; 4:2")
    assert [(s.book, s.start_chapter, s.start_verse) for s in spans] == [
        ("Jn", "3", "16"),
        ("Jn", "4", "2"),
    ]


def test_numbered_and_qualified_books():
    assert parse_reference("1 Jn 4:8")[0].book == "1 Jn"
    assert parse_reference("Sus (Th) 1:5")[0].book == "Sus (Th)"


@pytest.mark.parametrize("text", ["Jn ???", "Jn 3:16 see note"])
def test_unparseable_span_is_flagged_not_dropped(text):
    (span,) = parse_reference(text)
    assert not span.parsed
    assert span.type == ""


@pytest.mark.parametrize("text", [None, "", "   "])
def test_empty_reference(text):
    assert parse_reference(text) == []


def test_same_book_ignores_case_and_spaces():
    assert same_book("1 Jn", "1Jn")
    assert same_book("jn", "Jn")
    assert not same_book("Jn", "1 Jn")
