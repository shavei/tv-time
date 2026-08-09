# tv-time → Simkl importer

Rebuild a watch history on [Simkl](https://simkl.com/) after TV Time's shutdown.

Two ways to get your history in, and they share the same import pipeline:

1. **File import** — parse a `watched.csv` / `watched.json` export and mark
   everything in it as watched.
2. **Discovery mode** — no export? Answer a stream of yes/no questions about
   popular titles in the genres you like, and say how much of each you watched.

Both feed a local queue which is then pushed to `POST /sync/history` in batches,
with rate limiting, retries and a report of anything that could not be matched.

---

## Setup

```bash
git clone <this repo> && cd tv-time
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

You need a Simkl app (https://simkl.com/settings/developer/) with the redirect
URI set to `urn:ietf:wg:oauth:2.0:oob`. Keep the Client ID and Client Secret
handy — the script asks for them on first run and stores them in
`~/.simkl-importer/credentials.json` (mode `0600`, outside this repository).

> **Rotate any secret you have pasted into a chat, an issue or a commit.**
> Regenerate it from the app's Edit page; the script will just ask again.

## Run it

```bash
python -m simkl_importer                       # interactive menu
python -m simkl_importer --file watched.csv    # parse a file and import it
python -m simkl_importer --discover            # interactive discovery session
python -m simkl_importer --send                # push the saved queue
python -m simkl_importer --file watched.csv --dry-run   # parse + match, post nothing
```

First run walks you through authentication:

* **`--auth-flow oob`** (default) — opens `https://simkl.com/oauth/authorize`,
  you approve the app, Simkl shows a code, you paste it back. The script
  exchanges it at `POST /oauth/token` for an `access_token`. You can paste the
  whole redirect URL instead of just the code.
* **`--auth-flow pin`** — no client secret needed: enter a 5-character code at
  https://simkl.com/pin/ and the script polls until you approve it. Useful over
  SSH or on a headless box.

Tokens do not expire. Revoke one under
[Connected Apps](https://simkl.com/settings/connected-apps/), then re-run with
`--reauth`.

## Input file format

CSV or JSON. Column names are matched loosely, so most exports work as-is:

| Field | Accepted headers |
|---|---|
| title | `title`, `name`, `show`, `show_name`, `series`, `movie` |
| year | `year`, `release_year`, `first_aired`, `release_date` |
| type | `type`, `media_type`, `kind` (`tv` / `movie` / `anime`) |
| season | `season`, `season_number`, `s` |
| episode | `episode`, `episode_number`, `ep`, `e` |
| watched date | `watched_at`, `date_watched`, `last_watched`, `created_at` |
| ids | `imdb_id`, `tmdb_id`, `tvdb_id`, `simkl_id`, `mal_id`, `anidb_id` |
| rating | `rating`, `score`, `my_rating` |

See [`sample_data/`](sample_data/) for working examples. Notes:

* **One row per episode is expected** — TV Time exports look like that, and rows
  are collapsed back into a single show with `seasons[].episodes[]`.
* A row with **no season/episode** means "the whole title": Simkl marks every
  aired episode watched and files the show under Completed.
* A row with a **season but no episode** means that whole season.
* Titles like `The Wire - S02E05` are parsed if there are no season/episode
  columns.
* An `imdb_id` skips the search step entirely, which is faster and more accurate.

## Discovery mode

```
Which do you want to go through?   1) TV  2) Movies  3) Anime  4) all
Favourite shows or movies (comma separated, blank to skip).
Genres to browse (comma separated).
```

Favourites are looked up to seed the genre list, then popular titles are pulled
per genre (`/tv/genres/...`, `/movies/genres/...`, `/anime/genres/...`, sorted by
`popular-this-month`), optionally topped up from Simkl Trending. Anything already
on your account is filtered out first, then each remaining title is offered:

```
[7/120] Severance (2022)  (Drama, Mystery | Simkl 8.9)   <drama>
      Watched it? [y/N/s/b/q]: y
      How much? (all / s1 / 1-3 / s2e5 / s2e1-10, blank = all, x = never mind)
      > s1, s2e1-4
      queued: S01 (all), S02 x4ep
```

`s` skips the rest of that genre, `b` goes back one, `q` stops and keeps the
queue. The queue is written to disk after every answer, so a long session
survives Ctrl-C — resume with `--send`.

## Rate limiting and quotas

The Simkl developer rules are enforced client-side:

* **1 POST per second** (`/sync/history` calls are spaced ≥1.1s apart), GETs are
  throttled to ~4/s.
* Every request carries `client_id`, `app-name`, `app-version` query params plus
  `User-Agent`, `Content-Type` and `simkl-api-key` headers, and `Authorization:
  Bearer <token>` where the endpoint needs it.
* `429` is retried honouring `Retry-After`; `5xx` and network errors get
  exponential backoff (4 attempts).
* Requests are counted per UTC day against `--daily-limit` (default 1000, the
  free-tier app quota) and the run stops with a clear message before you hit the
  server-side limit. `--daily-limit 0` disables the guard if your app has been
  approved for more.
* Items are sent **in batches** (`--batch-size`, default 50 titles per POST), so
  a 2,000-title import is ~40 requests, not 2,000.
* Library reads use `/sync/all-items/{type}?extended=simkl_ids_only`, fetched
  sequentially and cached for 24h (`--refresh-library` to force).

## Unmatched titles

Before anything is written, every title is resolved to a Simkl ID via
`/search/{tv,movie,anime}` (or `/search/id` when an IMDb ID is present). Results
are cached in `~/.simkl-importer/match-cache.json`, so re-runs cost almost no
quota.

Anything that cannot be resolved confidently is **skipped, not guessed**, and
written to `unmatched.csv` with a reason. Fix the titles or add IMDb IDs and
re-run that file. Options:

* `--interactive-match` — show the closest hit and ask instead of skipping.
* `--no-resolve` — skip searching and let Simkl match on title+year alone.

Titles Simkl itself rejects come back in the `not_found` block of the response
and are appended to the same CSV.

## Command line reference

| Flag | Meaning |
|---|---|
| `--file PATH` | import a `watched.csv` / `watched.json` |
| `--discover` | run the interactive discovery session |
| `--send` | push the saved queue and exit |
| `--dry-run` | do everything except the POST |
| `--yes` | skip the confirmation prompt |
| `--auth-flow {oob,pin}` | authentication method (default `oob`) |
| `--reauth` | force a new access token |
| `--batch-size N` | titles per `/sync/history` request (default 50) |
| `--daily-limit N` | local request budget per UTC day (default 1000, `0` = off) |
| `--no-resolve` | do not pre-resolve IDs |
| `--interactive-match` | confirm weak title matches by hand |
| `--refresh-library` | re-download what is already on your account |
| `--unmatched-out PATH` | where to write the failure report |
| `--home PATH` | config directory (default `~/.simkl-importer`) |
| `--verbose` | log every HTTP request |

Credentials can also come from `SIMKL_CLIENT_ID`, `SIMKL_CLIENT_SECRET` and
`SIMKL_ACCESS_TOKEN`, which take precedence over the stored file.

## Tests

```bash
pip install pytest && python -m pytest tests -q
```

The suite is fully offline — parsing, payload shapes, batching and the progress
mini-language. Nothing in it touches the network.

## Layout

```
simkl_importer/
  cli.py         argparse + interactive menu
  auth.py        OAuth (oob) and PIN flows
  client.py      rate-limited HTTP client, retries, request budget
  config.py      credential/queue/cache storage under ~/.simkl-importer
  parsers.py     watched.csv / watched.json -> WatchItem
  matching.py    title -> Simkl ID resolution, with an on-disk cache
  discovery.py   the interactive yes/no session
  progress.py    "s1, s2e1-4" -> seasons/episodes
  sync.py        batching, POST /sync/history, reporting
  models.py      WatchItem and the Simkl payload shapes
```
