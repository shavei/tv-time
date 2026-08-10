# tv-time → Simkl importer

Rebuild a watch history on [Simkl](https://simkl.com/) after TV Time's shutdown.

Two ways to get your history in, and they share the same import pipeline:

1. **File import** — parse a `watched.csv` / `watched.json` export and mark
   everything in it as watched.
2. **Discovery mode** — no export? It opens a page in your browser showing a
   wall of real posters; click everything you've watched. Every answer teaches
   it: later rounds are ordered best-match first and never re-ask about
   something you passed on.

Both feed a local queue which is then pushed to Simkl in batches — watched
titles to `POST /sync/history`, ones you only want to *watch later* to
`POST /sync/add-to-list` — with rate limiting, retries and a report of anything
that could not be matched.

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
python -m simkl_importer --discover            # poster picker in your browser
python -m simkl_importer --for-you             # one at a time, matched to your taste
python -m simkl_importer --discover --tui     # stay in the terminal instead
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
| status | `status`, `watchlist` — `plantowatch` / `watch later` queues instead of marking watched |
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
* A `status` of `plantowatch`, `watchlist`, `watch later` or similar sends the
  row to Plan to Watch rather than marking it watched.

## Discovery mode

`python -m simkl_importer --discover` opens your browser and everything happens
there. The terminal prints the URL and then waits:

```
====================================================================
  Opening in your browser. If nothing appeared, paste this in:

    http://127.0.0.1:51423/?token=l4FqNheoDu3Auw8ZX9oa4TebQNVqT0YA

====================================================================
```

Three screens:

1. **Setup** — what to browse (TV / movies / anime), favourite titles, genre
   chips, **era**, **order by**, and how many per genre. Your Simkl account is
   read in the background while you fill this in, so the genres you tend to
   watch come back pre-selected.
2. **Building** — a live log while candidates are gathered: titles are pulled
   per genre (`/tv/genres/...`, `/movies/genres/...`, `/anime/genres/...`),
   optionally topped up from Simkl Trending, then filtered and ranked against
   your taste profile.
3. **Picking** — a grid of real posters, best match first. Click everything
   you've watched, filter by title or genre to find things, then **Continue**
   to say how much of each show you saw — `all` by default, or `s1`, `1-3`,
   `s2e5`, `s2e1-10`, or combinations like `s1, s2e1-4`. **Add to queue** hands
   it back to the terminal, which sends it to Simkl.

   Watched none of them? The button says **None of these** — that is a real
   answer. All of them are recorded as declined, so the next run offers
   something different instead of the same wall.

### Watched it, or want to watch it

Each poster cycles through three states as you click it:

| Clicks | State | Where it goes |
|---|---|---|
| once | **watched** (teal) | `POST /sync/history` — you say how much on the next screen |
| twice | **want to watch** (amber) | `POST /sync/add-to-list` with `"to": "plantowatch"` |
| three times | back to unselected | recorded as declined |

Plan-to-watch titles skip the "how much did you watch?" screen entirely —
there is nothing to answer. The terminal reports the two groups separately:

```
Ready to send: 14 title(s)
  as watched      : 9 - 3 movie(s), 6 show(s), 41 explicit episode(s)
  to Plan to Watch: 5
```

In `--tui` mode the same choice is the `l` answer at the prompt.

### Where things are from

Simkl's genre browse is worldwide by default, and that is a lot of Korean,
Chinese, Japanese and Indian drama if those are not what you watch. The
**Where from** chips filter by release country — pick one or several, or leave
it on *Anywhere*:

```bash
python -m simkl_importer --for-you --country us,gb
python -m simkl_importer --for-you --country us --no-anime
```

Each country is swept separately and the results pooled, so `us,gb` really does
mean both. Anime is its own section with no country of origin to filter on, so
`--no-anime` is how you leave it out.

### Era and order

These two matter more than they look, because the point is to remember what you
watched *years ago*:

* **Order by** defaults to **Best of all time** (Simkl's `rank`). The
  alternatives — `popular-this-month`, `popular-today` — return whatever is out
  right now, which is a list of things nobody could have watched yet.
* **Era** narrows to a decade (`2010s`, `2000s`, …). Pick the years you were
  actually watching and the grid fills with things you might genuinely
  recognise.

If the browser does not open by itself, paste the printed URL — the script
tries `webbrowser`, then the platform handler (`start` / `explorer` on Windows,
`open` on macOS, `xdg-open` on Linux) and tells you which of the two happened.

### No image is ever written to disk

The `<img>` tags point straight at Simkl's `wsrv.nl` CDN, so your browser
fetches the posters itself, at full quality, and its ordinary HTTP cache covers
Simkl's "cache images by URL" rule. Nothing but JSON crosses the local server.

### The local server

It binds `127.0.0.1` only, on a random free port, and every request needs the
one-time token in the URL — plus the `Host` header must be loopback, so a web
page you happen to have open cannot reach it. Bodies are size-capped, and it
shuts down as soon as you submit. Standard library only: no web framework.

`--web-port N` pins the port, `--no-browser` just prints the URL.

### Terminal alternative (`--tui`)

`--discover --tui` asks the setup questions in the terminal and keeps the
one-at-a-time walkthrough, drawing posters as 24-bit colour half-blocks:

```
▄▄▄▄▄▄▄▄▄▄  [7/120]
██▓▓▒▒░░██  Severance (2022)
██▓▓▒▒░░██  Drama, Mystery
██▓▓▒▒░░██  Simkl 8.9  IMDb 8.7
██▓▓▒▒░░██  ████████░░ 84% match
▀▀▀▀▀▀▀▀▀▀  from: drama

      Watched it? [y/N/s/b/q]: y
      How much? (all / s1 / 1-3 / s2e5 / s2e1-10, blank = all, x = never mind)
      > s1, s2e1-4
      queued: S01 (all), S02 x4ep
```

`n` (or just Enter) skips, `s` skips the rest of that genre, `b` goes back one,
`q` stops and keeps the queue. This mode needs Pillow, and *does* cache the
downscaled images in `~/.simkl-importer/posters/` — that is what Simkl's docs
ask for when an app fetches images itself. `--no-posters` turns them off,
`--poster-width N` changes the size (default 22 columns).

Either way the queue is written to disk after every answer, so a long session
survives Ctrl-C — resume with `--send`.

### What the match percentage means

It compares the *shape* of a title's genres against the shape of your profile
— cosine similarity between the title's genres and your affinity weights. It is
**absolute**: only a title covering what you actually watch approaches 100%,
and one sharing nothing reads 0% even if it is the best of a weak batch.

Two earlier versions got this wrong in different ways, and both made unrelated
titles look like strong matches:

* stretching scores across the pool, so the top item always read 100% and the
  bottom always 0% however badly everything fitted;
* then averaging affinity across a title's genres, which made **one** genre in
  common with a broad profile score as highly as an exact match.

Comparing shapes fixes both. For a crime-and-thriller profile: a crime thriller
scores in the nineties, a crime drama around sixty, a lone "Adventure" zero.

Rating and era only break ties between titles that match you equally well —
they can never push something above a title that actually fits your taste.

The percentage is hidden entirely when there is nothing to go on: no profile
yet, or no genres known for the title. If a whole batch comes back under 40%
the sweep says so rather than pretending.

### Taste profile

Every title you accept is recorded in `~/.simkl-importer/accepted.json` — which,
unlike the queue, is *not* cleared when you send. From it the script builds a
profile of genres and decades, and every "no" is remembered in `rejected.json`
as a negative signal.

Round two then:

* ranks candidates by genre affinity, with common genres damped by IDF so
  "drama" does not drown out "cyberpunk", plus small nudges for rating and for
  the decades you tend to watch;
* shows the resulting `84% match` bar and offers the best matches first;
* suggests genres to browse instead of making you think of them;
* never asks about anything you already said no to.

If there is nothing recorded yet but your account already has titles in it, the
first round seeds the profile from that library instead (up to 150 titles, genre
lookups cached forever in `genre-cache.json`).

`--no-taste` falls back to plain popularity order. `--forget-rejected` starts
asking about passed-over titles again.

### Never asked twice

Discovery excludes everything you have already answered, using four records —
no single one of them covers every path:

| File | Holds | Why the others are not enough |
|---|---|---|
| `library.json` | what is on your Simkl account | cached for 24h, so it lags a fresh import |
| `queue.json` | accepted, not yet sent | emptied by `--send` |
| `accepted.json` | **every** title ever accepted | survives the send — this is the durable record |
| `rejected.json` | every title declined | only covers the "no" answers |

The gap that matters is accept → `--send` → run discovery again: the queue is
now empty and the library cache still predates the import, so `accepted.json`
is the only thing standing between you and being asked all over again. A
successful send also drops the library cache, so the next run refetches from
the account rather than trusting a snapshot taken before the write.

Each run tells you what it skipped and why:

```
  14 title(s) to go through.
  Skipped 106 you have seen before: 51 already on your account,
  38 you already marked watched, 17 you already said no to.
```

To deliberately revisit something: `--forget-rejected` clears the "no" list, and
deleting `~/.simkl-importer/accepted.json` clears the "yes" list (your Simkl
account still filters anything actually imported).

## For You mode

```bash
python -m simkl_importer --for-you
```

Same idea as discovery, without the setup screen and without the grid. It reads
your account, derives the genres from your taste profile, and then just starts
showing you titles — one at a time, biggest match first:

```
┌───────────┐   ████████░░  84% match
│           │   Severance
│  poster   │   2022 · Drama, Mystery, Thriller · ★ 8.9
│           │
└───────────┘   [ Watched it ]  [ Want to watch ]  [ Not watched ]

                Y watched · L want to watch · N not watched · ← back
```

Each card carries a short synopsis, fetched for that title as you reach it and
cached forever afterwards — a hundred summaries up front would be a hundred
requests for cards you may never see. The next card's is prefetched while you
read, so it is usually there before you are. Simkl's overviews arrive with
markup in them (`<br><br>` between paragraphs, the odd entity), which is
stripped, and they are trimmed at a sentence boundary to roughly 400
characters — enough to recognise the thing, not the whole plot.

Say **Watched it** and it asks how much right there (`all` by default, with
`s1` / `s1-s2` / `s1-s3` shortcuts). Say **Want to watch** and it goes to Plan
to Watch. Say **Not watched** and it is remembered as declined.

`←` steps back if you misclick, and **Done — add them** finishes at any point.

**Stopping early is safe.** Titles you never got to are left untouched — only
the ones you actually answered are recorded, so the rest come round again next
time. (In the grid this is different, and correctly so: every card was on
screen, so anything left unselected genuinely means "not watched".)

### It shows the closest matches, not the first arrivals

For You shows **100 titles you have not answered before** (`--count N` to
change it) — but it looks at **four times that many** to choose them, then
keeps only the highest-matching ones:

```
  Looking at up to 400 unanswered title(s) to pick the 100 closest to your taste...
  Looked at 412, showing the 100 closest to your taste (71-100% match).
```

That gap is the whole point. Taking the first 100 that turn up gives you
whatever the pager happened to reach; taking the best 100 of 400 gives you a
list actually shaped by your profile. Your strongest genres are swept first, so
the pool leans that way before ranking even starts.

### Every genre is swept across every section

The sweep goes genre first, section second: `action` across TV, movies and
anime, then `adventure` across all three, and so on. Section first — all of TV,
then all of movies — meant TV's genres filled the target on their own and the
movie endpoints were never called at all, so a film could not reach the pool
however well it matched. A genre is never left half-swept either, since
stopping mid-genre would bias the pool towards whichever section is listed
first.

### It shows you what it learned

For You has no setup screen, so the top of the card carries the profile it is
working from:

```
Taste profile from 46 accepted title(s): action x21, thriller x14, crime x9
```

If that does not look like you, the recommendations will not either — and the
fix is answering **Watched it** on a run of things you know rather than tuning
anything. `--sort popular-this-month` is worth trying if the default all-time
`rank` ordering leans more towards prestige than the blockbusters you actually
watch.

### It digs until it finds enough

The genre endpoints are paginated, so rather than taking page one and giving
up, it keeps turning pages — across every genre evenly — until the pool is
full, then stops immediately so no quota is spent on rows you will never see.

This matters more than it sounds. After a few sessions you can easily have
several hundred titles answered, and page one of your favourite genres is
entirely stuff you have already dealt with:

```
  Looking for 100 title(s) you have not answered yet...
    tv/drama p1: 50 new (0 unanswered)
    ...
    tv/drama p7: 50 new (50 unanswered)
    tv/drama p8: 50 new (50 unanswered)
  100 title(s) to go through.
  Skipped 300 you have seen before: 300 you already said no to.
```

Digging deeper costs relevance nothing — everything found is ranked together
and only the best survive, so a title from page eight beats one from page one
if it matches you better. If a genre genuinely runs dry the sweep stops and
says so, and suggests `--forget-rejected` if the shortfall is mostly things you
declined.

The grid keeps to a single page per genre — you configure the sweep there, so
paging deeper would just multiply requests for rows nobody asked for.

It is also menu option **4**, next to the grid at **3**.

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
  a 2,000-title import is ~40 requests, not 2,000. Watched titles and
  plan-to-watch titles are batched separately, into their own endpoints.
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
| `--tui` | run discovery in the terminal instead of the browser |
| `--web-port N` | pin the picker's port (default: a free one) |
| `--no-browser` | print the picker URL instead of opening a browser |
| `--no-posters` | do not draw poster thumbnails in `--tui` mode |
| `--poster-width N` | poster width in terminal columns (default 22) |
| `--no-taste` | rank by popularity instead of your taste profile |
| `--for-you` | one at a time, matched to your taste, no setup screen |
| `--count N` | how many titles For You shows (default 100, chosen from 4× that many) |
| `--country CODES` | only titles released here, e.g. `us` or `us,gb` (default: anywhere) |
| `--no-anime` | leave anime out of For You |
| `--sort` | catalogue ordering: `rank` (all-time, default), `popular-this-month`, `popular-today` |
| `--forget-rejected` | ask again about titles you previously said no to |
| `--unmatched-out PATH` | where to write the failure report |
| `--home PATH` | config directory (default `~/.simkl-importer`) |
| `--verbose` | log every HTTP request |

Credentials can also come from `SIMKL_CLIENT_ID`, `SIMKL_CLIENT_SECRET` and
`SIMKL_ACCESS_TOKEN`, which take precedence over the stored file.

## Tests

```bash
pip install pytest && python -m pytest tests -q
```

The suite is fully offline — parsing, payload shapes, batching, the progress
mini-language, taste ranking, poster rendering, and the picker's HTTP server
(token checks, `Host` validation, commit handling) driven over a real socket on
loopback. Nothing in it reaches the internet.

## Layout

```
simkl_importer/
  cli.py         argparse + interactive menu
  auth.py        OAuth (oob) and PIN flows
  client.py      rate-limited HTTP client, retries, request budget
  config.py      credential/queue/cache storage under ~/.simkl-importer
  parsers.py     watched.csv / watched.json -> WatchItem
  matching.py    title -> Simkl ID resolution, with an on-disk cache
  discovery.py   candidate gathering + the terminal yes/no walkthrough
  web.py         the localhost poster picker (stdlib only)
  taste.py       taste profile from your answers, and candidate ranking
  images.py      poster thumbnails as terminal colour blocks (--tui)
  progress.py    "s1, s2e1-4" -> seasons/episodes
  sync.py        batching, POST /sync/history and /sync/add-to-list, reporting
  models.py      WatchItem and the Simkl payload shapes
```
