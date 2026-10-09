# biblindex-importer

Exports references to Bible verses (patristic quotations and allusions) from the
[BiblIndex API](https://www.biblindex.org/api) into a CSV file, one Bible book at a
time: for example, every quotation of the Gospel of John.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; on Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

Put your API credentials in `.env` (the file is gitignored; `.env.example` lists every
setting):

```ini
BIBLINDEX_CLIENT_ID=...
BIBLINDEX_CLIENT_SECRET=...
BIBLINDEX_USERNAME=...      # your biblindex.org login
BIBLINDEX_PASSWORD=...
BIBLINDEX_BIBLE_ID=8
```

The token is requested with the OAuth2 password grant (client ID and secret plus
your login), the same way the reference `biblindex-client` does. Without a
username and password, the tool falls back to the client-credentials grant, which
BiblIndex rejects with "Client authentication failed" for ordinary API clients.

Quotation data requires an API client account (`ROLE_API_CLIENT`). Request credentials
on your BiblIndex profile page. Books, authors and works are public.

## Usage

```bash
# All quotations of John -> output/jn_quotations.csv
python -m biblindex_importer export --book John

# Any other book: name, abbreviation, code or id all work
python -m biblindex_importer export --book Mt --output output/matthew.csv
python -m biblindex_importer books            # list books and their ids

# Quick trial run: first 20 quotations only
python -m biblindex_importer export --book John --limit 20

# Also resolve the quoting work of each quotation (slow, see below)
python -m biblindex_importer export --book John --with-works
```

After `pip install -e .`, `biblindex-import` is the same as `python -m biblindex_importer`.

Options: `--bible <id>` overrides `BIBLINDEX_BIBLE_ID`. `--refresh` ignores the cache.
`-v` turns on debug logging.

While it runs, the tool prints one dot per API response, 100 per row. Dots stop
only while it waits (rate limit, retries) or reads from the cache. `--no-dots`
turns them off, e.g. when output goes to a log file.

## Output

One row per quotation and per passage (sequence) of the book that it quotes. An
extract citing "Jn 3:16; Mt 5:3" produces one row for the John part only. The file is
UTF-8 with a BOM, so Excel shows Greek and Latin text correctly.

| Column | Meaning |
|---|---|
| `quotation_id` | BiblIndex quotation id |
| `reference` | Quoted span, e.g. `Jn 3:16-18` |
| `extract_reference` | Full quoted passage (may span several books) |
| `bible_id`, `start_book`, `start_chapter`, `start_verse`, `end_book`, `end_chapter`, `end_verse` | Coordinates in that Bible's numbering (verses may carry a letter: `16a`) |
| `words` | Word range inside the verse, e.g. `1-16` (empty when the whole verse is meant) |
| `sequence_type` | `verse`, `consecutive_verses`, `chapter`, … |
| `coordinates_from` | `sequence` (exact, from `/api/sequences`), `reference` (parsed from the reference text), or empty if the reference couldn't be parsed |
| `quotation_types` | e.g. quotation, allusion |
| `authors` | Author(s) of the quoting work |
| `quoted_author` | Author the quoted text is attributed to, if different |
| `content`, `description` | Quoted text as found in the source; analyst's note |
| `verification_status`, `verification_authority`, `source`, `apparatus_letter` | Curation metadata |
| `sequence_id`, `extract_id` | BiblIndex ids for further lookups |

Exact coordinates come from `/api/sequences`, which BiblIndex grants separately
from the rest of the corpus. If your account can't read it, the tool says so once
and parses the coordinates from each extract's reference text instead, e.g.
`[JB] Jn 1:1 w1-16`.

With `--with-works`, these columns are added. Values are shown for quotation
1321591, the same as on its page on biblindex.org:

| Column | Example |
|---|---|
| `work_title`, `work_latin_title` | Contra Iudaeos, gentiles et haereticos |
| `work_abbreviation`, `work_id` | IVD, 9431 |
| `edition`, `edition_name` | PG48, Patrologia Graeca 48 |
| `position` | `[IVD] 1 (col.77, l.38) - 1 (col.77, l.38)` |
| `pos_start_book` / `_chapter` / `_paragraph` / `_page` / `_subpage` / `_line`, and the same for `pos_end_*` | The position split into sortable fields |

`col.` is used for Patrologia Graeca/Latina, which are numbered in columns, and
`p.` for everything else.

## Rate limiting

BiblIndex allows authenticated clients **1 request per second per account**. The
client stays under that limit as follows:

- It waits `BIBLINDEX_MIN_INTERVAL` (default 1.1 s) between authenticated requests.
- It reads the `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers and pauses
  before the quota runs out. BiblIndex announces an ~8 s reset after every request
  while accepting 1 request/second, so these pauses are capped at
  `BIBLINDEX_MAX_RESET_WAIT` (default 2 s).
- On a `429`, it waits for `Retry-After` and slows down by 1.5× for the rest of the
  run (up to 10 s between requests). After 5 consecutive 429s it stops, so the
  account is not penalised further.
- Public reference data (books, authors, works) is fetched anonymously, outside
  the per-account limit, still at a polite 0.5 s interval.
- Cloudflare sometimes answers with a browser check instead of the API. The client
  detects this and retries with the token or after a pause. It never tries to
  solve the check.

At the end of each run it logs a summary: requests made, how many got a 429, cache
hits and time spent waiting.

## Cache and resuming

Every response is stored in `.cache/biblindex.sqlite3` (for 30 days by default). If
a run is interrupted, run the same command again: cached pages are replayed
instantly and only the missing ones are downloaded. Later books reuse already
resolved authors and works.

## How `--with-works` works and what it costs

The chain is: quotation → *patristic mention* (the position) → work edition →
work (title) and edition (e.g. PG48). Mentions can't be filtered by book, so for
each linked resource the tool picks the cheaper of two ways:

- **one by one:** 1 request per resource not yet cached. Used for trial runs such
  as `--limit 20`.
- **list the whole collection:** 100 per request. For John's 54,507 quotations
  this means listing all ~843,000 mentions in ~8,430 requests, instead of
  54,507 single requests. That takes about 2.5–4.5 hours, depending on
  `BIBLINDEX_MAX_RESET_WAIT`.

The listing is cached, so exporting further books afterwards needs no new mention
requests. If an interrupted run is restarted, it continues from the cache.

## Tests

```bash
pytest
```

The tests run against an in-memory fake of the API (pagination, filters, 429s,
Cloudflare challenges, token expiry), so they need no network or credentials.
