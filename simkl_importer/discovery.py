"""Interactive discovery: work out what you have watched by asking about it.

Rather than needing a complete export, this walks you through popular titles in
the genres you care about and asks "seen it?" one by one. Anything you say yes
to is queued for the same /sync/history import the file path uses.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Set

from .client import SimklClient, SimklError
from .config import Store
from .matching import candidate_ids
from .models import ANIME, MOVIE, TV, WatchItem, normalize_title
from .progress import ProgressError, apply_progress, parse_progress

SECTION_TO_TYPE = {"tv": TV, "movies": MOVIE, "anime": ANIME}
LIBRARY_ENDPOINTS = {"tv": "shows", "movies": "movies", "anime": "anime"}
LIBRARY_MAX_AGE = 24 * 3600

SUGGESTED_GENRES = [
    "action", "adventure", "animation", "comedy", "crime", "documentary",
    "drama", "family", "fantasy", "history", "horror", "mystery", "romance",
    "science-fiction", "thriller", "war", "western",
]

MENU_HELP = """
    y  yes, I watched it        n  no / skip
    s  skip the rest of this genre
    b  back to the previous title
    q  stop discovery and keep what I have queued so far
"""


# --------------------------------------------------------------------- helpers


def collect_simkl_ids(payload: Any, found: Optional[Set[str]] = None) -> Set[str]:
    """Walk an arbitrary sync response and pull out every simkl id it mentions."""
    found = found if found is not None else set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("simkl", "simkl_id") and isinstance(value, (int, str)):
                found.add(str(value))
            else:
                collect_simkl_ids(value, found)
    elif isinstance(payload, list):
        for entry in payload:
            collect_simkl_ids(entry, found)
    return found


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
        source="discovery",
    )


def format_candidate(candidate: Dict[str, Any]) -> str:
    bits = []
    genres = candidate.get("genres")
    if isinstance(genres, list) and genres:
        bits.append(", ".join(str(g) for g in genres[:3]))
    ratings = candidate.get("ratings") or {}
    simkl_rating = (ratings.get("simkl") or {}).get("rating")
    imdb_rating = (ratings.get("imdb") or {}).get("rating")
    if simkl_rating:
        bits.append(f"Simkl {simkl_rating}")
    if imdb_rating:
        bits.append(f"IMDb {imdb_rating}")
    return f"  ({' | '.join(bits)})" if bits else ""


# -------------------------------------------------------------------- library


def load_library_ids(client: SimklClient, store: Store, sections: Iterable[str], refresh: bool = False) -> Set[str]:
    """Simkl IDs already in the account, so we do not ask about them again.

    Cached for a day. Per Simkl's sync rules the three types are fetched
    sequentially (never in parallel) and with the smallest possible payload.
    """
    cached = store.load_library()
    fresh = cached and (time.time() - float(cached.get("fetched_at", 0)) < LIBRARY_MAX_AGE)
    if fresh and not refresh:
        print(f"  Using cached library ({len(cached.get('ids', []))} items already tracked).")
        return set(cached.get("ids", []))

    ids: Set[str] = set()
    print("  Fetching your existing Simkl library so we skip what you already track...")
    for section in sections:
        endpoint = LIBRARY_ENDPOINTS.get(section)
        if not endpoint:
            continue
        print(f"    - /sync/all-items/{endpoint} ... ", end="", flush=True)
        try:
            payload = client.library_ids(endpoint)
        except SimklError as exc:
            print(f"skipped ({exc})")
            continue
        section_ids = collect_simkl_ids(payload)
        ids |= section_ids
        print(f"{len(section_ids)} item(s)")

    store.save_library({"fetched_at": time.time(), "ids": sorted(ids)})
    return ids


# ------------------------------------------------------------------ candidates


def seed_from_favourites(client: SimklClient, titles: List[str]) -> Dict[str, Any]:
    """Look up favourite titles to (a) offer them and (b) learn their genres."""
    genres: Set[str] = set()
    hits: List[Dict[str, Any]] = []
    for title in titles:
        print(f"    looking up '{title}' ... ", end="", flush=True)
        found = None
        for media_type, search_type in ((TV, "tv"), (MOVIE, "movie"), (ANIME, "anime")):
            try:
                results = client.search(search_type, title, limit=3)
            except SimklError as exc:
                print(f"search failed ({exc})")
                results = []
            if results:
                found = (results[0], media_type)
                break
        if not found:
            print("no match")
            continue
        candidate, media_type = found
        hits.append({"candidate": candidate, "media_type": media_type})
        for genre in candidate.get("genres") or []:
            slug = str(genre).strip().lower().replace(" ", "-")
            if slug:
                genres.add(slug)
        print(f"{candidate.get('title')} ({candidate.get('year')})")
    return {"genres": sorted(genres), "hits": hits}


def gather_candidates(
    client: SimklClient,
    sections: List[str],
    genres: List[str],
    per_genre: int,
    sort: str = "popular-this-month",
) -> List[Dict[str, Any]]:
    """Pull popular titles per genre per section, de-duplicated by simkl id."""
    seen: Set[str] = set()
    gathered: List[Dict[str, Any]] = []

    for section in sections:
        for genre in genres:
            print(f"    {section}/{genre} ... ", end="", flush=True)
            try:
                results = client.genre_titles(section, genre=genre, sort=sort, limit=per_genre)
            except SimklError as exc:
                print(f"skipped ({exc})")
                continue
            kept = 0
            for candidate in results:
                ids = candidate_ids(candidate)
                simkl_id = str(ids.get("simkl") or "")
                if not simkl_id or simkl_id in seen:
                    continue
                seen.add(simkl_id)
                gathered.append({"candidate": candidate, "section": section, "genre": genre})
                kept += 1
            print(f"{kept} new")

    return gathered


def add_trending(client: SimklClient, sections: List[str], seen: Set[str], limit: int = 30) -> List[Dict[str, Any]]:
    """Simkl Trending (the pre-built CDN files) as an extra candidate source."""
    gathered: List[Dict[str, Any]] = []
    for section in sections:
        print(f"    Simkl Trending {section} ... ", end="", flush=True)
        results = client.trending(section, "month")
        kept = 0
        for candidate in results[:limit]:
            ids = candidate_ids(candidate)
            simkl_id = str(ids.get("simkl") or "")
            if not simkl_id or simkl_id in seen:
                continue
            seen.add(simkl_id)
            gathered.append({"candidate": candidate, "section": section, "genre": "trending"})
            kept += 1
        print(f"{kept} new")
    return gathered


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


def run_discovery(
    client: SimklClient,
    store: Store,
    existing: Optional[List[WatchItem]] = None,
    refresh_library: bool = False,
) -> List[WatchItem]:
    """Interactive session. Returns the queue of confirmed watched items."""
    queue: List[WatchItem] = list(existing or [])
    queued_keys: Set[Any] = {item.dedupe_key() for item in queue}

    print("\n" + "=" * 60)
    print("Discovery mode - let's work out what you have watched")
    print("=" * 60)

    print("\nWhich do you want to go through?")
    print("  1) TV shows   2) Movies   3) Anime   4) all of them")
    choice = ask("  Choose [4]: ", "4")
    sections = {
        "1": ["tv"],
        "2": ["movies"],
        "3": ["anime"],
        "4": ["tv", "movies", "anime"],
    }.get(choice, ["tv", "movies", "anime"])

    print("\nFavourite shows or movies (comma separated, blank to skip).")
    print("  These get offered first and are used to guess your genres.")
    favourites = parse_list(ask("  > "))

    seed_genres: List[str] = []
    favourite_hits: List[Dict[str, Any]] = []
    if favourites:
        seeds = seed_from_favourites(client, favourites)
        seed_genres = seeds["genres"]
        favourite_hits = seeds["hits"]
        if seed_genres:
            print(f"  Genres picked up from your favourites: {', '.join(seed_genres[:8])}")

    print("\nGenres to browse (comma separated). Blank uses the ones above,")
    print(f"  or pick from: {', '.join(SUGGESTED_GENRES)}")
    genres = parse_list(ask("  > ")) or seed_genres or ["all"]
    genres = [genre.lower().replace(" ", "-") for genre in genres]

    per_genre = ask("\nHow many titles per genre? [20]: ", "20")
    try:
        per_genre = max(1, min(50, int(per_genre)))
    except ValueError:
        per_genre = 20

    library = load_library_ids(client, store, sections, refresh=refresh_library)

    print("\n  Building the candidate list...")
    entries: List[Dict[str, Any]] = []
    for hit in favourite_hits:
        entries.append({"candidate": hit["candidate"], "section": "favourites", "genre": "favourites"})
    entries.extend(gather_candidates(client, sections, genres, per_genre))

    seen_ids = {str(candidate_ids(entry["candidate"]).get("simkl")) for entry in entries}
    if ask("\n  Also include Simkl Trending titles? [y/N]: ").lower().startswith("y"):
        entries.extend(add_trending(client, sections, seen_ids))

    # drop things already tracked on the account or already queued
    filtered: List[Dict[str, Any]] = []
    already = 0
    for entry in entries:
        simkl_id = str(candidate_ids(entry["candidate"]).get("simkl") or "")
        if simkl_id and simkl_id in library:
            already += 1
            continue
        filtered.append(entry)

    print(f"\n  {len(filtered)} title(s) to go through ({already} already in your Simkl library, skipped).")
    if not filtered:
        return queue
    print(MENU_HELP)

    index = 0
    skip_genre: Optional[str] = None
    added = 0
    while index < len(filtered):
        entry = filtered[index]
        candidate, section, genre = entry["candidate"], entry["section"], entry["genre"]

        if skip_genre is not None and genre == skip_genre:
            index += 1
            continue
        skip_genre = None

        media_type = SECTION_TO_TYPE.get(section, TV)
        item = candidate_to_item(candidate, media_type)
        if item is None:
            index += 1
            continue
        if item.dedupe_key() in queued_keys:
            index += 1
            continue

        counter = f"[{index + 1}/{len(filtered)}]"
        print(f"{counter} {item.label()}{format_candidate(candidate)}   <{genre}>")
        answer = ask("      Watched it? [y/N/s/b/q]: ", "n").lower()

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
        if answer.startswith("y"):
            if ask_progress(item):
                queue.append(item)
                queued_keys.add(item.dedupe_key())
                added += 1
                store.save_queue([queued.to_dict() for queued in queue])
        index += 1

    print(f"\n  Discovery done: {added} title(s) added, {len(queue)} in the queue overall.")
    return queue
