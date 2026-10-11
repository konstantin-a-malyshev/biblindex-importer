import csv

import pytest

from biblindex_importer import cli
from biblindex_importer.config import ConfigError, Settings

ENV_VARS = [
    "BIBLINDEX_BASE_URL",
    "BIBLINDEX_CLIENT_ID",
    "BIBLINDEX_CLIENT_SECRET",
    "BIBLINDEX_USERNAME",
    "BIBLINDEX_PASSWORD",
    "BIBLINDEX_BIBLE_ID",
    "BIBLINDEX_LOCALE",
    "BIBLINDEX_MIN_INTERVAL",
    "BIBLINDEX_PUBLIC_MIN_INTERVAL",
    "BIBLINDEX_MAX_RESET_WAIT",
    "BIBLINDEX_CACHE_PATH",
    "BIBLINDEX_CACHE_TTL_DAYS",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_without_env_file(tmp_path):
    settings = Settings.from_env(tmp_path / "missing.env")
    assert settings.bibles == "all"
    assert settings.min_interval == 1.1
    assert settings.max_reset_wait == 2.0
    assert not settings.has_credentials


def test_env_file_is_read(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "BIBLINDEX_CLIENT_ID=abc\nBIBLINDEX_CLIENT_SECRET=xyz\nBIBLINDEX_BIBLE_ID=3\n"
        "BIBLINDEX_BASE_URL=https://example.org/\nBIBLINDEX_MIN_INTERVAL=\n"
    )
    settings = Settings.from_env(env)
    assert settings.has_credentials
    assert settings.bibles == "3"
    assert settings.base_url == "https://example.org"
    assert settings.min_interval == 1.1  # empty value falls back to the default


def test_invalid_number_is_reported(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BIBLINDEX_BIBLE_ID=eight\n")
    with pytest.raises(ConfigError):
        Settings.from_env(env)


@pytest.fixture
def cli_env(tmp_path, biblindex_cli_api):
    env = tmp_path / ".env"
    env.write_text(
        "BIBLINDEX_CLIENT_ID=id\nBIBLINDEX_CLIENT_SECRET=secret\n"
        "BIBLINDEX_BASE_URL=https://biblindex.test\n"
        "BIBLINDEX_MIN_INTERVAL=0\nBIBLINDEX_PUBLIC_MIN_INTERVAL=0\n"
        f"BIBLINDEX_CACHE_PATH={tmp_path / 'cache.sqlite3'}\n"
    )
    return env


@pytest.fixture
def biblindex_cli_api(api):
    api.add_collection(
        "/api/bibles",
        [{"@id": "/api/bibles/8", "id": 8, "abbreviation": "JB", "name": "Bible de Jerusalem"},
         {"@id": "/api/bibles/4", "id": 4, "abbreviation": "NTG", "name": "Novum Testamentum"}],
    )
    api.add_collection(
        "/api/books",
        [{"@id": "/api/books/50", "id": 50, "abbreviation": "Jn", "code": "43N",
          "name": "John", "slug": "jn"}],
    )
    api.add_collection(
        "/api/sequences",
        [{"@id": "/api/sequences/1", "id": 1, "name": "Jn 1:1", "bible": "/api/bibles/8",
          "startBook": "/api/books/50", "startChapter": "1", "startVerse": "1",
          "_f": {"bible": 8, "startBook": 50}}],
    )
    api.add_collection(
        "/api/extracts",
        [{"@id": "/api/extracts/10", "id": 10, "abbreviation": "Jn 1:1",
          "sequences": ["/api/sequences/1"], "_f": {"bible": 8, "startBook": 50}}],
    )
    api.add_collection(
        "/api/quotations",
        [{"@id": "/api/quotations/100", "id": 100, "extract": "/api/extracts/10",
          "authors": [], "quotationTypes": [], "content": "In principio",
          "_f": {"bible": 8, "startBook": 50}}],
    )
    api.add_collection("/api/quotation_types", [])
    return api


def test_cli_export_writes_csv(cli_env, tmp_path, biblindex_cli_api):
    out = tmp_path / "john.csv"
    code = cli.main(["--env-file", str(cli_env), "export", "--book", "John", "--output", str(out)])
    assert code == 0
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["quotation_id"], r["reference"]) for r in rows] == [("100", "Jn 1:1")]


def test_cli_bible_option_overrides_env(cli_env, tmp_path, biblindex_cli_api):
    out = tmp_path / "john.csv"
    cli.main(["--env-file", str(cli_env), "export", "--book", "Jn", "--bible", "5",
              "--output", str(out)])
    params = biblindex_cli_api.calls_to("/api/quotations")[0][1]
    assert params["bible"] == "5"


def test_cli_unknown_book_fails(cli_env, biblindex_cli_api):
    assert cli.main(["--env-file", str(cli_env), "export", "--book", "Hezekiah"]) == 1


def test_cli_export_requires_credentials(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"BIBLINDEX_CACHE_PATH={tmp_path / 'c.sqlite3'}\n")
    assert cli.main(["--env-file", str(env), "export", "--book", "John"]) == 2


def test_cli_lists_books(cli_env, biblindex_cli_api, capsys):
    assert cli.main(["--env-file", str(cli_env), "books"]) == 0
    assert "John" in capsys.readouterr().out


def test_request_marks_wrap_and_end_lines():
    import io
    import logging

    from biblindex_importer.client import EVENT_CACHE, EVENT_ERROR, EVENT_OK

    stream = io.StringIO()
    marks = cli.RequestMarks(stream, width=3)
    for event in (EVENT_OK, EVENT_CACHE, EVENT_ERROR, EVENT_OK, EVENT_OK):
        marks(event)
    assert stream.getvalue() == ".!x\n.."
    handler = cli.MarkAwareHandler(marks)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(logging.LogRecord("x", logging.INFO, "", 0, "hello", None, None))
    assert stream.getvalue() == ".!x\n..\nhello\n"
    marks.end_line()  # nothing pending: no blank line
    assert stream.getvalue().endswith("hello\n")


def _dot_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line and set(line) <= {".", "!", "x"}]


def test_cli_prints_a_dot_per_response(cli_env, biblindex_cli_api, capsys):
    cli.main(["--env-file", str(cli_env), "books"])
    assert _dot_lines(capsys.readouterr().err) == ["."]  # one page of books


def test_cli_marks_cache_hits(cli_env, biblindex_cli_api, capsys):
    cli.main(["--env-file", str(cli_env), "books"])
    capsys.readouterr()
    cli.main(["--env-file", str(cli_env), "books"])  # same request, now cached
    assert _dot_lines(capsys.readouterr().err) == ["!"]


def test_cli_no_dots(cli_env, biblindex_cli_api, capsys):
    cli.main(["--env-file", str(cli_env), "--no-dots", "books"])
    assert len(biblindex_cli_api.calls) == 1  # a real request was made...
    assert _dot_lines(capsys.readouterr().err) == []  # ...but no dot printed


@pytest.mark.parametrize("value", ["all", "ALL", "8", "8,4,2", " 8 , 4 "])
def test_bible_spec_accepts_all_and_id_lists(tmp_path, value):
    env = tmp_path / ".env"
    env.write_text(f"BIBLINDEX_BIBLE_ID={value}\n")
    assert Settings.from_env(env).bibles == value.strip()


def test_bible_spec_rejects_names(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BIBLINDEX_BIBLE_ID=JB\n")
    with pytest.raises(ConfigError, match="8,4,2"):
        Settings.from_env(env)


def test_cli_exports_all_bibles_by_default(cli_env, biblindex_cli_api):
    cli.main(["--env-file", str(cli_env), "export", "--book", "John"])
    queried = {c[1]["bible"] for c in biblindex_cli_api.calls_to("/api/quotations")}
    assert queried == {"8", "4"}


def test_cli_lists_bibles(cli_env, biblindex_cli_api, capsys):
    assert cli.main(["--env-file", str(cli_env), "bibles"]) == 0
    out = capsys.readouterr().out
    assert "JB" in out and "NTG" in out
