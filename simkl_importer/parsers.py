"""Read a local watched.csv / watched.json into WatchItem objects.

TV Time and its various export tools do not agree on column names, and a show
export is usually one row *per episode*. So we do two things here:

  1. map a generous set of header aliases onto our fields, and
  2. collapse many episode rows back into one show with seasons/episodes.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import ANIME, MOVIE, TV, WatchItem

# header alias -> canonical field
FIELD_ALIASES: Dict[str, str] = {
    "title": "title",
    "name": "title",
    "show": "title",
    "show_name": "title",
    "show title": "title",
    "series": "title",
    "series_name": "title",
    "movie": "title",
    "movie_name": "title",
    "original_title": "title",
    "year": "year",
    "release_year": "year",
    "first_aired": "year",
    "released": "year",
    "release_date": "year",
    "type": "media_type",
    "media_type": "media_type",
    "kind": "media_type",
    "category": "media_type",
    "season": "season",
    "season_number": "season",
    "season_num": "season",
    "s": "season",
    "episode": "episode",
    "episode_number": "episode",
    "episode_num": "episode",
    "ep": "episode",
    "e": "episode",
    "watched_at": "watched_at",
    "date_watched": "watched_at",
    "watched_date": "watched_at",
    "last_watched": "watched_at",
    "last_watched_at": "watched_at",
    "created_at": "watched_at",
    "updated_at": "watched_at",
    "status": "status",
    "watchlist": "status",
    "rating": "rating",
    "score": "rating",
    "my_rating": "rating",
    "imdb": "imdb",
    "imdb_id": "imdb",
    "imdb id": "imdb",
    "tmdb": "tmdb",
    "tmdb_id": "tmdb",
    "tvdb": "tvdb",
    "tvdb_id": "tvdb",
    "thetvdb_id": "tvdb",
    "simkl": "simkl",
    "simkl_id": "simkl",
    "mal": "mal",
    "mal_id": "mal",
    "anidb": "anidb",
    "anidb_id": "anidb",
    "episode_id": "_ignore",
    "id": "_ignore",
}

MOVIE_WORDS = {"movie", "movies", "film", "films", "feature"}
ANIME_WORDS = {"anime"}
SHOW_WORDS = {"tv", "show", "shows", "series", "tv_show", "tvshow", "tv show", "episode"}

# "S02E05", "2x05", "Season 2 Episode 5"
SXXEYY = re.compile(r"\bs(?:eason)?\s*(\d{1,3})\s*(?:[ex]|\s*episode\s*)\s*(\d{1,4})\b", re.I)
NxN = re.compile(r"\b(\d{1,3})\s*x\s*(\d{1,4})\b")
YEAR_IN_TITLE = re.compile(r"\((19|20)(\d{2})\)\s*$")


class ParseError(RuntimeError):
    pass


# --------------------------------------------------------------------- values


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def to_int(value: Any) -> Optional[int]:
    text = _clean(value)
    if not text:
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group()) if match else None


def to_year(value: Any) -> Optional[int]:
    text = _clean(value)
    if not text:
        return None
    match = re.search(r"(19|20)\d{2}", text)
    return int(match.group()) if match else None


def to_utc_iso(value: Any) -> Optional[str]:
    """Best-effort date parsing into the ``2011-09-01T14:44:00Z`` Simkl wants."""
    text = _clean(value)
    if not text:
        return None
    candidate = text.replace("/", "-")
    formats = (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%m-%d-%Y",
        "%b %d, %Y",
        "%d %b %Y",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def guess_media_type(row: Dict[str, Any]) -> str:
    declared = _clean(row.get("media_type")).lower()
    if declared:
        if declared in MOVIE_WORDS:
            return MOVIE
        if declared in ANIME_WORDS:
            return ANIME
        if declared in SHOW_WORDS:
            return TV
    if row.get("season") is not None or row.get("episode") is not None:
        return TV
    if row.get("mal") or row.get("anidb"):
        return ANIME
    return MOVIE if declared in MOVIE_WORDS else TV


def split_title_and_episode(title: str) -> Tuple[str, Optional[int], Optional[int]]:
    """Pull S/E out of titles like ``The Wire - S02E05`` when there are no columns."""
    match = SXXEYY.search(title) or NxN.search(title)
    if not match:
        return title.strip(" -–\t"), None, None
    season, episode = int(match.group(1)), int(match.group(2))
    cleaned = (title[: match.start()] + title[match.end() :]).strip(" -–:\t")
    return cleaned or title.strip(), season, episode


# ---------------------------------------------------------------- row mapping


def map_headers(headers: Iterable[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for header in headers:
        if header is None:
            continue
        key = re.sub(r"[\s\-]+", "_", header.strip().lower())
        canonical = FIELD_ALIASES.get(key) or FIELD_ALIASES.get(key.replace("_", " "))
        if canonical and canonical != "_ignore":
            mapping[header] = canonical
    return mapping


def normalize_row(raw: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    for header, value in raw.items():
        canonical = mapping.get(header)
        if not canonical:
            continue
        text = _clean(value)
        if not text:
            continue
        if canonical == "year":
            row["year"] = to_year(text)
        elif canonical in ("season", "episode", "rating"):
            row[canonical] = to_int(text)
        elif canonical == "watched_at":
            row["watched_at"] = to_utc_iso(text)
        else:
            row[canonical] = text
    return row


def row_to_item(row: Dict[str, Any]) -> Optional[WatchItem]:
    title = _clean(row.get("title"))
    if not title:
        return None

    season, episode = row.get("season"), row.get("episode")
    if season is None and episode is None:
        title, season, episode = split_title_and_episode(title)

    year = row.get("year")
    if year is None:
        match = YEAR_IN_TITLE.search(title)
        if match:
            year = int(match.group(0).strip("() "))
            title = YEAR_IN_TITLE.sub("", title).strip()

    ids = {}
    for key in ("simkl", "imdb", "tmdb", "tvdb", "mal", "anidb"):
        value = _clean(row.get(key))
        if value:
            ids[key] = int(value) if key != "imdb" and value.isdigit() else value

    item = WatchItem(
        title=title,
        media_type=guess_media_type({**row, "season": season, "episode": episode}),
        year=year,
        ids=ids,
        status=_clean(row.get("status")).lower() or None,
        watched_at=row.get("watched_at"),
        rating=row.get("rating"),
        source="file",
    )
    if item.media_type != MOVIE and season is not None:
        item.add_episode(season, episode, row.get("watched_at"))
    return item


# ------------------------------------------------------------------ file I/O


def _sniff_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def read_csv(path: Path) -> List[Dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        reader = csv.DictReader(handle, dialect=_sniff_dialect(sample))
        if not reader.fieldnames:
            raise ParseError(f"{path} has no header row.")
        mapping = map_headers(reader.fieldnames)
        if "title" not in mapping.values():
            raise ParseError(
                f"{path}: could not find a title column among {reader.fieldnames}. "
                "Rename the column to 'title' (or show/series/movie/name)."
            )
        return [normalize_row(raw, mapping) for raw in reader]


def _flatten_json(payload: Any) -> List[Dict[str, Any]]:
    """Accept a bare list, or a dict keyed by type/section, or {"items": [...]}."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        raise ParseError("JSON must be a list of records or an object of lists.")

    rows: List[Dict[str, Any]] = []
    for key, value in payload.items():
        if not isinstance(value, list):
            continue
        hint = key.lower()
        for entry in value:
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            entry.setdefault("type", hint)
            rows.append(entry)
    if not rows:
        raise ParseError("No records found in the JSON file.")
    return rows


def read_json(path: Path) -> List[Dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_rows = _flatten_json(payload)

    rows: List[Dict[str, Any]] = []
    for raw in raw_rows:
        flat = dict(raw)
        # nested {"ids": {...}} and {"show": {...}} blocks, Simkl-export style
        nested_ids = flat.pop("ids", None)
        if isinstance(nested_ids, dict):
            flat.update({f"{k}_id": v for k, v in nested_ids.items()})
        for container in ("show", "movie", "anime"):
            nested = flat.pop(container, None)
            if isinstance(nested, dict):
                inner_ids = nested.pop("ids", None)
                if isinstance(inner_ids, dict):
                    flat.update({f"{k}_id": v for k, v in inner_ids.items()})
                for key, value in nested.items():
                    flat.setdefault(key, value)
                flat.setdefault("type", container)
        mapping = map_headers(flat.keys())
        rows.append(normalize_row(flat, mapping))
    return rows


def load_file(path: Path) -> List[WatchItem]:
    """Parse a file and collapse per-episode rows into one item per title."""
    path = Path(path)
    if not path.exists():
        raise ParseError(f"No such file: {path}")

    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".txt"):
        rows = read_csv(path)
    elif suffix == ".json":
        rows = read_json(path)
    else:
        raise ParseError(f"Unsupported file type '{suffix}' - use .csv or .json.")

    merged: Dict[Any, WatchItem] = {}
    order: List[Any] = []
    skipped = 0
    for row in rows:
        item = row_to_item(row)
        if item is None:
            skipped += 1
            continue
        key = item.dedupe_key()
        if key in merged:
            merged[key].merge(item)
        else:
            merged[key] = item
            order.append(key)

    if skipped:
        print(f"  (skipped {skipped} row(s) with no usable title)")
    return [merged[key] for key in order]
