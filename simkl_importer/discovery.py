"""Interactive discovery: work out what you have watched by asking about it.

Rather than needing a complete export, this walks you through titles and asks
"seen it?" one by one. Anything you say yes to is queued for the same
/sync/history import the file path uses.

Later rounds are not a repeat of the first: everything you have accepted builds
a taste profile, everything you rejected is remembered and never asked about
again, and candidates are ordered best-match first with the poster shown
alongside.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .client import SimklClient, SimklError
from .config import Store
from .images import PosterRenderer
from .matching import candidate_ids
from .models import ANIME, MOVIE, PLAN, TV, WatchItem
from .progress import ProgressError, apply_progress, parse_progress
from .taste import (
    GenreLookup,
    TasteProfile,
    build_profile,
    candidate_genres,
    rank_entries,
)

SECTION_TO_TYPE = {"tv": TV, "movies": MOVIE, "anime": ANIME}
TYPE_TO_SECTION = {TV: "tv", MOVIE: "movies", ANIME: "anime"}
LIBRARY_ENDPOINTS = {"tv": "shows", "movies": "movies", "anime": "anime"}
LIBRARY_MAX_AGE = 24 * 3600

# how many already-tracked titles we are willing to look up genres for when
# seeding a taste profile from scratch
MAX_LIBRARY_SEED = 150

# how many pages deep we are willing to go per genre when filling a target
MAX_PAGES = 8
# how many unanswered candidates a taste-driven run aims to put in front of you
DEFAULT_TARGET = 100

SUGGESTED_GENRES = [
    "action", "adventure", "animation", "comedy", "crime", "documentary",
    "drama", "family", "fantasy", "history", "horror", "mystery", "romance",
    "science-fiction", "thriller", "war", "western",
]

SORT_CHOICES = [
    ("rank", "Best of all time"),
    ("popular-this-month", "Popular this month"),
    ("popular-today", "Popular today"),
]

ERA_CHOICES = [
    ("all-years", "Any"),
    ("2020s", "2020s"),
    ("2010s", "2010s"),
    ("2000s", "2000s"),
    ("1990s", "1990s"),
    ("1980s", "1980s"),
]

MENU_HELP = """
    y  yes, I watched it        n  no / skip (remembered, never asked again)
    l  not yet - add it to Plan to Watch
    s  skip the rest of this genre
    b  back to the previous title
    q  stop discovery and keep what I have queued so far
"""


# --------------------------------------------------------------------- helpers


def ask(prompt: str, default: str = "") -> str:
    answer = input(prompt).strip()
    return answer or default


def parse_list(text: str) -> List[str]:
    return [chunk.strip() for chunk in text.replace(";", ",").split(",") if chunk.strip()]


def candidate_to_item(candidate: Dict[str, Any], media_type: str) -> Optional[WatchItem]:
    title = candidate.get("title")
    if not title:
        return None
    ids = candidate_ids(candidate)
    if not ids.get("simkl"):
        return None
    return WatchItem(
        title=str(title),
        media_type=media_type,
        year=candidate.get("year"),
        ids=ids,
        poster=str(candidate.get("poster") or ""),
        genres=candidate_genres(candidate),
        source="discovery",
    )


def detail_lines(candidate: Dict[str, Any], item: WatchItem, match: Optional[float], genre: str) -> List[str]:
    lines = [f"\033[1m{item.label()}\033[0m"]

    genres = candidate_genres(candidate)
    if genres:
        lines.append(", ".join(g.title() for g in genres[:4]))

    ratings = candidate.get("ratings") or {}
    bits = []
    simkl_rating = (ratings.get("simkl") or {}).get("rating")
    imdb_rating = (ratings.get("imdb") or {}).get("rating")
    if simkl_rating:
        bits.append(f"Simkl {simkl_rating}")
    if imdb_rating:
        bits.append(f"IMDb {imdb_rating}")
    if bits:
        lines.append("  ".join(bits))

    if match is not None:
        filled = int(round(match / 10))
        bar = "█" * filled + "░" * (10 - filled)
        lines.append(f"{bar} {match:.0f}% match")

    lines.append(f"from: {genre}")
    return lines


# -------------------------------------------------------------------- library


def load_library_map(
    client: SimklClient, store: Store, sections: Iterable[str],
    refresh: bool = False, log=print,
) -> Dict[str, str]:
    """Simkl ID -> media type for everything already on the account.

    Cached for a day. Per Simkl's sync rules the types are fetched sequentially
    (never in parallel) with the smallest payload the API offers.
    """
    cached = store.load_library()
    fresh = cached and (time.time() - float(cached.get("fetched_at", 0)) < LIBRARY_MAX_AGE)
    if fresh and not refresh and isinstance(cached.get("items"), dict):
        log(f"  Using cached library ({len(cached['items'])} items already tracked).")
        return dict(cached["items"])

    items: Dict[str, str] = {}
    log("  Fetching your existing Simkl library so we skip what you already track...")
    for section in sections:
        endpoint = LIBRARY_ENDPOINTS.get(section)
        if not endpoint:
            continue
        try:
            payload = client.library_ids(endpoint)
        except SimklError as exc:
            log(f"    - {endpoint}: skipped ({exc})")
            continue
        found = _extract_ids(payload)
        for simkl_id in found:
            items[simkl_id] = SECTION_TO_TYPE.get(section, TV)
        log(f"    - {endpoint}: {len(found)} item(s)")

    store.save_library({"fetched_at": time.time(), "items": items})
    return items


def _extract_ids(payload: Any, found: Optional[Set[str]] = None) -> Set[str]:
    """Walk an arbitrary sync response and pull out every simkl id it mentions."""
    found = found if found is not None else set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("simkl", "simkl_id") and isinstance(value, (int, str)):
                found.add(str(value))
            else:
                _extract_ids(value, found)
    elif isinstance(payload, list):
        for entry in payload:
            _extract_ids(entry, found)
    return found


# ------------------------------------------------------------------ candidates


def seed_from_favourites(client: SimklClient, titles: List[str], log=print) -> Dict[str, Any]:
    """Look up favourite titles to (a) offer them and (b) learn their genres."""
    genres: Set[str] = set()
    hits: List[Dict[str, Any]] = []
    for title in titles:
        found = None
        for media_type, search_type in ((TV, "tv"), (MOVIE, "movie"), (ANIME, "anime")):
            try:
                results = client.search(search_type, title, limit=3)
            except SimklError as exc:
                log(f"    '{title}': search failed ({exc})")
                results = []
            if results:
                found = (results[0], media_type)
                break
        if not found:
            log(f"    '{title}': no match")
            continue
        candidate, media_type = found
        hits.append({"candidate": candidate, "media_type": media_type})
        for genre in candidate_genres(candidate):
            slug = genre.replace(" ", "-")
            if slug:
                genres.add(slug)
        log(f"    '{title}' -> {candidate.get('title')} ({candidate.get('year')})")
    return {"genres": sorted(genres), "hits": hits}


def gather_candidates(
    client: SimklClient,
    sections: List[str],
    genres: List[str],
    per_genre: int,
    sort: str = "rank",
    year: str = "all-years",
    seen: Optional[Set[str]] = None,
    exclude: Optional[Set[str]] = None,
    target: Optional[int] = None,
    max_pages: int = MAX_PAGES,
    log=print,
) -> List[Dict[str, Any]]:
    """Pull titles per genre per section, de-duplicated by simkl id.

    These endpoints are paginated, and page one of a handful of genres is a few
    hundred titles at most - which runs dry fast once you have answered for a
    while. When `target` is set we keep turning pages until that many
    *unanswered* candidates have been found, counting against `exclude`, and
    stop as soon as we have enough so nobody pays for pages they will not see.
    """
    seen = seen if seen is not None else set()
    exclude = exclude or set()
    gathered: List[Dict[str, Any]] = []
    fresh = 0

    # Without a target there is nothing to fill, so one page each - paging
    # deeper would multiply the request count for rows nobody asked for.
    # With one: page 1 of every genre, then page 2 of every genre, so a
    # shortfall is made up across the board rather than exhausting the first.
    depth = max(1, max_pages) if target is not None else 1
    for page in range(1, depth + 1):
        exhausted = True
        for section in sections:
            for genre in genres:
                if target is not None and fresh >= target:
                    return gathered
                try:
                    results = client.genre_titles(
                        section, genre=genre, sort=sort, limit=per_genre,
                        page=page, year=year,
                    )
                except SimklError as exc:
                    log(f"    {section}/{genre} p{page}: skipped ({exc})")
                    continue
                if results:
                    exhausted = False
                kept = new_here = 0
                for candidate in results:
                    simkl_id = str(candidate_ids(candidate).get("simkl") or "")
                    if not simkl_id or simkl_id in seen:
                        continue
                    seen.add(simkl_id)
                    gathered.append({"candidate": candidate, "section": section, "genre": genre})
                    kept += 1
                    if simkl_id not in exclude:
                        fresh += 1
                        new_here += 1
                if kept:
                    suffix = f" ({new_here} unanswered)" if target is not None else ""
                    log(f"    {section}/{genre} p{page}: {kept} new{suffix}")
        if exhausted:
            break  # every genre returned an empty page; there is no more

    return gathered


def add_trending(client: SimklClient, sections: List[str], seen: Set[str],
                 limit: int = 30, log=print) -> List[Dict[str, Any]]:
    """Simkl Trending (the pre-built CDN files) as an extra candidate source."""
    gathered: List[Dict[str, Any]] = []
    for section in sections:
        results = client.trending(section, "month")
        kept = 0
        for candidate in results[:limit]:
            simkl_id = str(candidate_ids(candidate).get("simkl") or "")
            if not simkl_id or simkl_id in seen:
                continue
            seen.add(simkl_id)
            gathered.append({"candidate": candidate, "section": section, "genre": "trending"})
            kept += 1
        log(f"    Simkl Trending {section}: {kept} new")
    return gathered


# ----------------------------------------------------------------- taste seed


def build_taste_profile(
    client: SimklClient,
    store: Store,
    queue: List[WatchItem],
    library: Dict[str, str],
    enabled: bool = True,
    log=print,
) -> TasteProfile:
    """Learn from the current queue, everything accepted before, and rejections."""
    if not enabled:
        return TasteProfile()

    lookup = GenreLookup(client, store)

    accepted = [WatchItem.from_dict(raw) for raw in store.load_accepted()]
    known = {item.dedupe_key() for item in accepted}
    for item in queue:
        if item.dedupe_key() not in known:
            accepted.append(item)
            known.add(item.dedupe_key())

    # first run after an import: nothing recorded locally, but the account
    # itself is evidence of taste
    if not accepted and library:
        seed_ids = list(library.items())[:MAX_LIBRARY_SEED]
        uncached = [i for i, _ in seed_ids if i not in lookup.cache]
        if uncached:
            log(f"  Learning your taste from {len(seed_ids)} title(s) already on your account...")
        for simkl_id, media_type in seed_ids:
            genres = lookup.get(simkl_id, media_type)
            if genres:
                accepted.append(WatchItem(title=simkl_id, media_type=media_type, genres=genres))

    lookup.enrich(accepted, log=log)

    rejected = store.load_rejected()
    rejected_genres = [entry.get("genres", []) for entry in rejected.values() if isinstance(entry, dict)]

    profile = build_profile(accepted, rejected_genres)
    log(profile.summary())
    return profile


# ----------------------------------------------------------------- the prompt


def ask_progress(item: WatchItem) -> bool:
    """Ask how much of a show was watched. Returns False if the user backs out."""
    if item.is_movie:
        return True
    print("      How much? (all / s1 / 1-3 / s2e5 / s2e1-10, blank = all, x = never mind)")
    while True:
        answer = ask("      > ", "all")
        if answer.lower() in ("x", "cancel"):
            return False
        try:
            segments = parse_progress(answer)
        except ProgressError as exc:
            print(f"      Sorry, {exc}. Try again, e.g. 's1-s3' or 's2e5'.")
            continue
        apply_progress(item, segments)
        print(f"      queued: {item.describe_progress()}")
        return True


ALL_SECTIONS = ["tv", "movies", "anime"]


def prepare_taste(
    client: SimklClient,
    store: Store,
    queue: List[WatchItem],
    refresh_library: bool = False,
    use_taste: bool = True,
    log=print,
) -> Dict[str, Any]:
    """The part that can run before we know what you want to browse.

    Fetching the library and learning your taste needs no answers from you, so
    the web UI kicks this off the moment it starts and fills in the suggested
    genres once it lands.
    """
    library = load_library_map(client, store, ALL_SECTIONS, refresh=refresh_library, log=log)
    profile = build_taste_profile(client, store, queue, library, enabled=use_taste, log=log)
    return {
        "library": library,
        "profile": profile,
        "suggested": profile.top_genres(6),
        "summary": "" if profile.empty else profile.summary().strip(),
    }


def build_session(
    client: SimklClient,
    store: Store,
    queue: List[WatchItem],
    prepared: Dict[str, Any],
    sections: List[str],
    favourites: Optional[List[str]] = None,
    genres: Optional[List[str]] = None,
    per_genre: int = 20,
    include_trending: bool = False,
    sort: str = "rank",
    year: str = "all-years",
    target: Optional[int] = None,
    log=print,
) -> Dict[str, Any]:
    """Gather, filter and rank candidates. No prompting - all answers passed in."""
    library = prepared["library"]
    profile = prepared["profile"]
    queued_keys: Set[Any] = {item.dedupe_key() for item in queue}
    rejected: Dict[str, Any] = store.load_rejected()
    already_accepted: Set[str] = store.accepted_ids()

    seed_genres: List[str] = []
    favourite_hits: List[Dict[str, Any]] = []
    if favourites:
        seeds = seed_from_favourites(client, favourites, log=log)
        seed_genres = seeds["genres"]
        favourite_hits = seeds["hits"]

    chosen = genres or seed_genres or prepared.get("suggested") or ["all"]
    chosen = [genre.lower().replace(" ", "-") for genre in chosen]
    per_genre = max(1, min(50, int(per_genre or 20)))

    log("  Building the candidate list...")
    seen_ids: Set[str] = set()
    entries: List[Dict[str, Any]] = []
    for hit in favourite_hits:
        simkl_id = str(candidate_ids(hit["candidate"]).get("simkl") or "")
        if simkl_id and simkl_id not in seen_ids:
            seen_ids.add(simkl_id)
            entries.append(
                {
                    "candidate": hit["candidate"],
                    "section": TYPE_TO_SECTION.get(hit["media_type"], "tv"),
                    "genre": "favourites",
                }
            )
    # everything already answered, so paging can aim at a number of titles you
    # have actually not seen rather than a number of rows fetched
    answered_ids: Set[str] = set(library) | set(rejected) | set(already_accepted)
    for item in queue:
        if item.ids.get("simkl"):
            answered_ids.add(str(item.ids["simkl"]))

    if target:
        log(f"  Looking for {target} title(s) you have not answered yet...")
    entries.extend(
        gather_candidates(
            client, sections, chosen, per_genre, sort=sort, year=year,
            seen=seen_ids, exclude=answered_ids, target=target, log=log,
        )
    )

    if include_trending:
        entries.extend(add_trending(client, sections, seen_ids, log=log))

    # Drop everything already answered. Four separate records, because no one
    # of them survives every path: the library can be a day stale, the queue is
    # emptied on send, and rejections and acceptances each only cover one answer.
    filtered: List[Dict[str, Any]] = []
    in_library = said_no = said_yes = in_queue = 0
    for entry in entries:
        simkl_id = str(candidate_ids(entry["candidate"]).get("simkl") or "")
        if simkl_id and simkl_id in library:
            in_library += 1
            continue
        if simkl_id and simkl_id in rejected:
            said_no += 1
            continue
        if simkl_id and simkl_id in already_accepted:
            said_yes += 1
            continue
        item = candidate_to_item(entry["candidate"], SECTION_TO_TYPE.get(entry["section"], TV))
        if item is None:
            continue
        if item.dedupe_key() in queued_keys:
            in_queue += 1
            continue
        filtered.append(entry)

    ranked = rank_entries(filtered, profile)

    reasons = []
    if in_library:
        reasons.append(f"{in_library} already on your account")
    if said_yes:
        reasons.append(f"{said_yes} you already marked watched")
    if said_no:
        reasons.append(f"{said_no} you already said no to")
    if in_queue:
        reasons.append(f"{in_queue} already queued")

    skipped = in_library + said_no + said_yes + in_queue
    log(f"  {len(ranked)} title(s) to go through.")
    if skipped:
        log(f"  Skipped {skipped} you have seen before: {', '.join(reasons)}.")
    if not ranked:
        log("  Nothing new to ask about.")
        if said_no:
            log(f"  {said_no} of those were titles you declined before - "
                "run with --forget-rejected to be asked about them again.")
        log("  Otherwise try more genres, a different era, or Simkl Trending.")
    elif target and len(ranked) < target:
        log(f"  Only {len(ranked)} left unanswered in these genres "
            f"(wanted {target}) - widen the genres or era for more.")
    if ranked and not profile.empty:
        log("  Ordered best-match first based on what you have already accepted.")

    return {
        "ranked": ranked,
        "profile": profile,
        "rejected": rejected,
        "queued_keys": queued_keys,
        "in_library": in_library,
        "said_no": said_no,
        "said_yes": said_yes,
        "in_queue": in_queue,
        "skipped": skipped,
        "skip_reasons": reasons,
    }


def collect_session(
    client: SimklClient,
    store: Store,
    queue: List[WatchItem],
    refresh_library: bool = False,
    use_taste: bool = True,
) -> Dict[str, Any]:
    """Terminal setup questions, then build the ranked candidate list."""
    print("\nWhich do you want to go through?")
    print("  1) TV shows   2) Movies   3) Anime   4) all of them")
    choice = ask("  Choose [4]: ", "4")
    sections = {
        "1": ["tv"],
        "2": ["movies"],
        "3": ["anime"],
        "4": list(ALL_SECTIONS),
    }.get(choice, list(ALL_SECTIONS))

    prepared = prepare_taste(client, store, queue, refresh_library, use_taste)

    suggested = prepared["suggested"]
    if suggested:
        print(f"\n  Suggested genres based on that: {', '.join(suggested)}")

    print("\nFavourite shows or movies (comma separated, blank to skip).")
    favourites = parse_list(ask("  > "))

    default_genres = suggested or ["all"]
    print(f"\nGenres to browse - blank accepts: {', '.join(default_genres)}")
    print(f"  or pick from: {', '.join(SUGGESTED_GENRES)}")
    genres = parse_list(ask("  > "))

    raw_per_genre = ask("\nHow many titles per genre? [20]: ", "20")
    try:
        per_genre = int(raw_per_genre)
    except ValueError:
        per_genre = 20

    print("\nWhich era? (blank = any)")
    print("  e.g. 2020s, 2010s, 2000s, 1990s")
    year = ask("  > ", "all-years") or "all-years"

    trending = ask("\n  Also include Simkl Trending titles? [y/N]: ").lower().startswith("y")

    return build_session(
        client,
        store,
        queue,
        prepared,
        sections=sections,
        favourites=favourites,
        genres=genres,
        per_genre=per_genre,
        include_trending=trending,
        year=year,
    )


def run_discovery(
    client: SimklClient,
    store: Store,
    existing: Optional[List[WatchItem]] = None,
    refresh_library: bool = False,
    renderer: Optional[PosterRenderer] = None,
    use_taste: bool = True,
    session: Optional[Dict[str, Any]] = None,
) -> List[WatchItem]:
    """Terminal walkthrough. Returns the queue of confirmed watched items."""
    queue: List[WatchItem] = list(existing or [])

    if session is None:
        # the caller usually prints the banner and builds the session for us
        print("\n" + "=" * 60)
        print("Discovery mode - let's work out what you have watched")
        print("=" * 60)
        session = collect_session(client, store, queue, refresh_library, use_taste)
    if renderer and not renderer.enabled and renderer.reason:
        print(f"  (posters off: {renderer.reason})")
    ranked = session["ranked"]
    profile = session["profile"]
    rejected = session["rejected"]
    queued_keys = session["queued_keys"]
    if not ranked:
        return queue
    print(MENU_HELP)

    index = 0
    skip_genre: Optional[str] = None
    added = 0
    while index < len(ranked):
        entry, match = ranked[index]
        candidate, section, genre = entry["candidate"], entry["section"], entry["genre"]

        if skip_genre is not None and genre == skip_genre:
            index += 1
            continue
        skip_genre = None

        item = candidate_to_item(candidate, SECTION_TO_TYPE.get(section, TV))
        if item is None or item.dedupe_key() in queued_keys:
            index += 1
            continue

        counter = f"[{index + 1}/{len(ranked)}]"
        lines = detail_lines(candidate, item, match if not profile.empty else None, genre)
        lines.insert(0, counter)
        print()
        if renderer:
            print(renderer.render_beside(item.poster, lines))
        else:
            print("\n".join(lines))

        answer = ask("      Watched it? [y/N/l/s/b/q]: ", "n").lower()

        if answer.startswith("q"):
            print("\n  Stopping discovery.")
            break
        if answer.startswith("b"):
            index = max(0, index - 1)
            continue
        if answer.startswith("s"):
            skip_genre = genre
            print(f"      Skipping the rest of '{genre}'.")
            index += 1
            continue
        if answer.startswith("l"):
            item.intent = PLAN
            queue.append(item)
            queued_keys.add(item.dedupe_key())
            added += 1
            store.save_queue([queued.to_dict() for queued in queue])
            _remember_accepted(store, item)
            print("      queued: plan to watch")
            index += 1
            continue
        if answer.startswith("y"):
            if ask_progress(item):
                queue.append(item)
                queued_keys.add(item.dedupe_key())
                added += 1
                store.save_queue([queued.to_dict() for queued in queue])
                _remember_accepted(store, item)
        else:
            simkl_id = str(item.ids.get("simkl") or "")
            if simkl_id:
                rejected[simkl_id] = {"title": item.title, "genres": item.genres}
                store.save_rejected(rejected)
        index += 1

    print(f"\n  Discovery done: {added} title(s) added, {len(queue)} in the queue overall.")
    return queue


def _remember_accepted(store: Store, item: WatchItem) -> None:
    """Keep accepted titles for taste, separately from the queue.

    The queue is emptied once it has been sent; this list is what makes the
    next round smarter, so it outlives it.
    """
    accepted = store.load_accepted()
    key = str(item.ids.get("simkl") or item.title)
    if any(str(raw.get("ids", {}).get("simkl") or raw.get("title")) == key for raw in accepted):
        return
    accepted.append(item.to_dict())
    store.save_accepted(accepted)
