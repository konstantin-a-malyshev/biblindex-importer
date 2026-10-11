# CLAUDE.md

Python CLI that exports patristic quotations of a Bible book from the BiblIndex
REST API into CSV. README.md has user-facing usage; this file has what you need to
work on the code.

## Commands (Windows, venv in `.venv`)

```bash
.venv/Scripts/python -m pytest                                         # all tests, offline
.venv/Scripts/python -m ruff check src tests --select F,E,W,B,I --line-length 100
.venv/Scripts/python -m biblindex_importer books                       # public, no credentials
.venv/Scripts/python -m biblindex_importer export --book John --limit 20
```

Setup: `python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]" ruff`.
`uv` is not installed on this machine.

## Layout (`src/biblindex_importer/`)

- `config.py`: `Settings.from_env()` reads `.env` via `dotenv_values` (it does not
  mutate `os.environ`; real env vars win). All settings are prefixed `BIBLINDEX_`.
- `rate_limiter.py`: `RateLimiter` with an injectable clock/sleep. It paces by a
  minimum interval, `X-RateLimit-*` headers and 429 `Retry-After`, and slows down
  1.5× per 429.
- `client.py`: `BiblIndexClient` handles the OAuth2 token, retries, Cloudflare
  detection, the SQLite cache and Hydra pagination (`iter_collection` follows
  `view.next`). It knows nothing about the domain. There are two limiters:
  `auth_limiter` (rate-limited account) and `public_limiter` (anonymous).
- `cache.py`: `ResponseCache` keyed by canonical GET URL. It is what makes runs
  resumable.
- `books.py`: book list (public) and `find_book` (id / name / abbreviation / code /
  slug).
- `exporter.py`: `BookExporter` joins quotations → extracts → sequences into rows,
  plus `--with-works` resolution. It also contains `write_csv`. Every row gets all
  `COORDINATE_COLUMNS` keys (empty when unknown).
- `references.py`: pure parser for reference strings such as `[JB] Jn 1:1 w1-16`.
  It is the fallback when sequences are not readable.
- `cli.py`: argparse subcommands `export`, `books` and `bibles`. `RequestMarks`
  prints a mark per request via the client's `on_event` hook: `.` for `EVENT_OK`,
  `!` for `EVENT_CACHE` and `x` for `EVENT_ERROR` (any status ≥ 400 or network
  failure, including retried ones). `MarkAwareHandler` ends a row of marks before
  each log line. `--no-dots` disables them.

Tests use `tests/conftest.py::FakeApi`, an in-memory fake server on `responses`
that paginates and filters through a private `_f` dict per item, with
`queue()` for injecting 429 / 401 / 5xx / Cloudflare responses. Use the
`make_client` fixture, which runs on a fake clock so nothing really sleeps. Add new
API behaviour to FakeApi rather than mocking individual URLs.

## BiblIndex API facts (learned the hard way)

- **Spec:** the HTML docs page (`/en/api-documentation`) sits behind a Cloudflare
  browser check and can't be fetched by tools. Get the full OpenAPI spec with
  `curl -H "Accept: application/vnd.openapi+json" https://www.biblindex.org/api/docs.jsonopenapi`.
  Do not try to bypass Cloudflare challenges.
- **Auth:** `POST /api/token`, form-encoded → `Authorization: Bearer`. The spec says
  "client credentials", but in practice the **password grant** is needed
  (`grant_type=password`, `username`, `password`, `client_id`, `client_secret`), as in
  the reference client github.com/janalis/biblindex-python-client. A
  `client_credentials` request with valid ID and secret gets
  `401 Client authentication failed` (observed 2026-10-09).
  The quotation corpus (quotations, extracts, sequences, verses, patristic
  mentions, quotation types) needs `ROLE_API_CLIENT`. Books, authors, works and
  work editions are public.
- **Cloudflare:** the `python-requests/x.y` User-Agent gets a managed challenge
  (403 HTML, `_cf_chl_opt`) on every endpoint, authenticated or not. The tool's own
  honest UA (`client.USER_AGENT`) passes. This was verified 2026-10-09: same token
  and same URL, 403 vs 200. Keep `session.headers["User-Agent"] = USER_AGENT` as an
  assignment; `setdefault` silently keeps the python-requests UA. Never work around
  a challenge (no browser UA spoofing, no harvested `cf_clearance` cookies, no
  solvers). If one appears with the tool's UA, it's for BiblIndex to fix; the error
  includes the CF-RAY for their admins. A 522 is an origin timeout, not a challenge,
  and is retried.
- **Per-resource grants:** the user's account (2026-10-09) can read quotations,
  extracts, patristic mentions, quotation types and verses, but **not sequences**
  (403 "Access Denied." on both list and item). The exporter detects this once
  (`sequences_readable`) and parses coordinates from the extract `abbreviation`
  with `references.parse_reference`. Each row's `coordinates_from` says
  `sequence`, `reference` or empty (unparsed).
- **Extract `abbreviation` shapes seen live:** `[JB] Jn 17:21`, `[JB] Jn 16:27-28`,
  `[JB] Jn 1:1 w1-16` (words) and `[JB] Jn 4` (whole chapter). `[JB]` is a
  Bible marker. John has 68,993 extracts in bible 8.
- **Size:** John (book 50) in bible 8 = 54,507 quotations (2026-10-09). A full
  core export is ~550 quotation pages plus extract/sequence pages, roughly 30 min.
  For `--with-works`, listing all 842,949 patristic mentions (~8,430 pages) beats
  54,507 single fetches.
- **Rate-limit headers are over-conservative:** every response says
  `x-ratelimit-limit: 1`, `remaining: 0` and a reset ~8–9 s in the future, yet
  requests ~1.1–1.6 s apart all get 200 (measured 2026-10-09, 5 in a row). Fully
  trusting the reset made a trial run ~7 s/request. Header waits are therefore capped
  (`BIBLINDEX_MAX_RESET_WAIT`, default 2 s). 429 + `Retry-After` stays uncapped and
  binding. If 429s start appearing, lower confidence in this finding before raising
  speed.
- **Rate limit:** 1 request/s per account for authenticated calls. Anonymous calls
  are outside the policy but get Cloudflare-challenged sporadically. The client then
  retries with the token.
- **Pagination:** 30 items/page by default, `itemsPerPage` ≤ 100 (`/api/verses` ≤ 30).
  JSON-LD responses use `member`, `totalItems` and `view.next`; the `hydra()`
  helper also accepts the `hydra:` prefixed forms.
- **Data model:** a quotation has an `extract`, an extract has `sequences`, and a
  sequence has the coordinates (`startBook`, `startChapter`, `startVerse`, `end*`,
  `bible`). Chapters and verses are strings (`"16a"`, `"Mand4.1"`).
- **Book filter:** `bible=<id>&startBook=<id>` works on quotations, extracts and
  sequences. Always pass `bible` together with `startBook`, otherwise it is a slow
  table scan.
- **Works and position:** a quotation → `patristicMention` (holds the position:
  start/end Book, Chapter, Paragraph, Page, SubPage, Line; needs the API-client role;
  cannot be filtered by book) → `workEdition` → `work` (`name`, `abbreviation` e.g.
  `IVD`) and `edition` (`abbreviation` e.g. `PG48`, `collection`). The website's
  "Position" `[IVD] 1 (col.77, l.38) - 1 (col.77, l.38)` is reproduced by
  `format_position`; this was verified live for quotations 1321591, 1324499 and
  1378501. `BookExporter.resolve_all` fetches IRIs one by one or lists the whole
  collection, whichever takes fewer requests.
  The user said author, work and position are the most important data. A
  "by-work" strategy existed once and was removed because it gives no position.
- **Data quality of the full John export (2026-10-11, all Bibles, 72,466 rows):**
  - Whole-book references (`[JB] Jn`): 149. Whole-chapter references: 63.
  - Verses past the end of their chapter (e.g. `[Vg] Jn 15:33`): 65.
  - Chapters above 21 (e.g. `[NTG] Jn 74:12`): 25. These are BiblIndex data errors.
  - Unparseable references: 6 (`[JB] Jn:33`, `[JB] Jn 20:17-?`, `[NTG] Jn 4:35-C`, …).
  - Patristic mentions with no position fields: 2.
  - Works without an abbreviation (position has no `[ABBR]` prefix): 1,033 works,
    25,812 rows.
  - The website's verse-range search (JB 54,338; NRSV pivot 72,268) seems to skip
    whole-book and out-of-range references. That rule reproduces the JB gap of 169
    exactly, but is 16 off for all Bibles.
- **IDs:** John = book 50, Matthew 47, Mark 48, Luke 49.
- **Bibles matter for completeness:** every quotation is indexed in the Bible its
  author quoted. For John (2026-10-11): 8 JB (French) 54,507, 4 NTG (Greek NT) 11,892,
  2 Vg (Vulgate) 6,053, 14 NTS (Syriac) 14. The sum is 72,466, which equals the
  count with no bible filter, so there is no overlap. The website search for
  Jn 1:1–21:25 says 72,268. Bible 8 alone (the old default, from the docs'
  example) missed 25%. The default is now `BIBLINDEX_BIBLE_ID=all`: one
  indexed `bible`+`startBook` query per Bible (21 Bibles), never an unfiltered
  scan. `/api/bibles` is public and works with the tool's UA (its earlier
  Cloudflare block was the python-requests UA).
- **Scale:** about 5,988 works and 9,287 work editions in total.

## Not yet verified against the live API (no credentials yet)

- Why the API has ~200 more John quotations (72,466) than the website search
  (72,268). One guess is that the website's verse range drops chapter-only or
  unusual references. Check it by comparing a per-chapter count from the CSV
  with the website.
- How the website joins book/chapter/paragraph divisions in a position (we use
  `.`, as in `OS.1.4` for Vulgate quotation 1608701). Only paragraph-only
  positions have been compared so far.

When credentials become available, check these first with a `--limit` run, and
update this section.

## Rules

- Never read, print or commit `.env` (gitignored; `.env.example` is the template).
  Never log tokens or token responses (`access_token`); log only `expires_in`/`token_type`.
  `.cache/`, `output/` and `.venv/` are gitignored too.
- Keep the client polite: any new request path must go through
  `BiblIndexClient._request` so it is paced, cached and retried. Don't add
  concurrency.
- Live calls during development: public endpoints only, a handful at a time.
  Never run a full export just to test something.
- The design should stay book-agnostic: nothing John-specific outside tests and
  docs.
- Tests must never write to the real `output/` or `.cache/`. CLI tests run in
  `tmp_path` (autouse `monkeypatch.chdir`). This was learned when a test overwrote
  `output/jn_quotations.csv`.
