"""Data model for a single "I watched this" record, plus payload rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Simkl treats anime as its own media type, but the /sync/history payload only
# has "movies", "shows" and "episodes" buckets -- anime goes in "shows".
MOVIE = "movie"
TV = "tv"
ANIME = "anime"

VALID_STATUSES = {"watching", "plantowatch", "completed", "dropped", "hold"}

# What we intend to do with an item: record it as watched (POST /sync/history)
# or just park it on a watchlist (POST /sync/add-to-list).
WATCHED = "watched"
PLAN = "plantowatch"
VALID_INTENTS = {WATCHED, PLAN}

# ids we know how to pass through to Simkl (anything else is dropped so we
# never send junk keys the API has to guess at)
KNOWN_ID_KEYS = {"simkl", "imdb", "tmdb", "tvdb", "mal", "anidb", "slug"}


def normalize_title(title: str) -> str:
    """Loose title key used for de-duplication and cache lookups."""
    lowered = title.strip().lower()
    lowered = re.sub(r"\s*\((19|20)\d{2}\)\s*$", "", lowered)  # trailing "(2013)"
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return lowered.strip()


@dataclass
class Episode:
    number: int
    watched_at: Optional[str] = None
    ids: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"number": self.number}
        if self.watched_at:
            payload["watched_at"] = self.watched_at
        if self.ids:
            payload["ids"] = dict(self.ids)
        return payload


@dataclass
class Season:
    number: int
    episodes: List[Episode] = field(default_factory=list)
    watched_at: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"number": self.number}
        if self.watched_at:
            payload["watched_at"] = self.watched_at
        if self.episodes:
            episodes = sorted(self.episodes, key=lambda e: e.number)
            payload["episodes"] = [e.to_payload() for e in episodes]
        return payload


@dataclass
class WatchItem:
    """One movie, or one show together with everything watched in it."""

    title: str
    media_type: str = TV
    year: Optional[int] = None
    ids: Dict[str, Any] = field(default_factory=dict)
    seasons: Dict[int, Season] = field(default_factory=dict)
    status: Optional[str] = None
    watched_at: Optional[str] = None
    rating: Optional[int] = None
    source: str = "file"
    poster: str = ""
    genres: List[str] = field(default_factory=list)
    intent: str = WATCHED
    # set by the matcher; None until we have tried to resolve it
    matched: Optional[bool] = None
    match_note: str = ""

    # ---------------------------------------------------------------- helpers

    @property
    def is_movie(self) -> bool:
        return self.media_type == MOVIE

    @property
    def is_plan(self) -> bool:
        """Parked on a watchlist rather than marked watched."""
        return self.intent == PLAN

    @property
    def bucket(self) -> str:
        """Which /sync/history array this item belongs in."""
        return "movies" if self.is_movie else "shows"

    @property
    def whole_thing(self) -> bool:
        """True when we are marking the entire title watched, not episodes."""
        return not self.seasons

    def dedupe_key(self):
        if self.ids.get("simkl"):
            return ("simkl", str(self.ids["simkl"]))
        if self.ids.get("imdb"):
            return ("imdb", str(self.ids["imdb"]))
        return (self.media_type, normalize_title(self.title), self.year)

    def label(self) -> str:
        year = f" ({self.year})" if self.year else ""
        return f"{self.title}{year}"

    def describe_progress(self) -> str:
        if self.is_plan:
            return "plan to watch"
        if self.is_movie:
            return "movie"
        if self.whole_thing:
            return f"whole show ({self.status or 'completed'})"
        parts = []
        for number in sorted(self.seasons):
            season = self.seasons[number]
            if season.episodes:
                eps = sorted(e.number for e in season.episodes)
                parts.append(f"S{number:02d} x{len(eps)}ep")
            else:
                parts.append(f"S{number:02d} (all)")
        return ", ".join(parts)

    def add_episode(
        self,
        season_number: int,
        episode_number: Optional[int] = None,
        watched_at: Optional[str] = None,
        episode_ids: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a watched season (episode_number=None) or single episode."""
        season = self.seasons.get(season_number)
        if season is None:
            season = Season(number=season_number)
            self.seasons[season_number] = season
        if episode_number is None:
            # whole season: an explicit episode list would only narrow it
            season.episodes = []
            season.watched_at = season.watched_at or watched_at
            return
        if season.watched_at and not season.episodes:
            return  # already covered by a whole-season entry
        if any(e.number == episode_number for e in season.episodes):
            return
        season.episodes.append(
            Episode(number=episode_number, watched_at=watched_at, ids=episode_ids or {})
        )

    def merge(self, other: "WatchItem") -> None:
        """Fold another record for the same title into this one."""
        self.year = self.year or other.year
        for key, value in other.ids.items():
            self.ids.setdefault(key, value)
        self.status = self.status or other.status
        self.watched_at = self.watched_at or other.watched_at
        self.rating = self.rating if self.rating is not None else other.rating
        # actually watching it beats planning to
        if other.intent == WATCHED:
            self.intent = WATCHED
        self.poster = self.poster or other.poster
        if other.genres and not self.genres:
            self.genres = list(other.genres)
        for number, season in other.seasons.items():
            if not season.episodes:
                self.add_episode(number, None, season.watched_at)
                continue
            for episode in season.episodes:
                self.add_episode(number, episode.number, episode.watched_at, episode.ids)

    # ---------------------------------------------------------------- payload

    def clean_ids(self) -> Dict[str, Any]:
        return {k: v for k, v in self.ids.items() if k in KNOWN_ID_KEYS and v}

    def to_list_payload(self) -> Dict[str, Any]:
        """Item shape for POST /sync/add-to-list."""
        payload: Dict[str, Any] = {"title": self.title, "to": self.intent}
        if self.year:
            payload["year"] = self.year
        ids = self.clean_ids()
        if ids:
            payload["ids"] = ids
        return payload

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"title": self.title}
        if self.year:
            payload["year"] = self.year
        ids = self.clean_ids()
        if ids:
            payload["ids"] = ids
        # A show-level watched_at applies to the *whole* title, so only send it
        # when that is what we mean - otherwise the per-season/per-episode
        # timestamps below carry the dates.
        if self.watched_at and (self.is_movie or self.whole_thing):
            payload["watched_at"] = self.watched_at
        if self.rating is not None:
            payload["rating"] = self.rating

        if self.is_movie:
            return payload

        if self.seasons:
            payload["seasons"] = [
                self.seasons[n].to_payload() for n in sorted(self.seasons)
            ]
            if self.status and self.status in VALID_STATUSES:
                payload["status"] = self.status
        else:
            # No episode detail: let Simkl mark everything aired as watched.
            payload["status"] = self.status if self.status in VALID_STATUSES else "completed"

        if self.media_type == ANIME and not self.seasons:
            # Anime seasons are separate series on Simkl; this flag makes a
            # TVDB/TMDB-identified anime mark every season from the first.
            if any(k in payload.get("ids", {}) for k in ("tvdb", "tmdb")):
                payload["use_tvdb_anime_seasons"] = True

        return payload

    # ------------------------------------------------------------ (de)serialise

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "media_type": self.media_type,
            "year": self.year,
            "ids": self.ids,
            "status": self.status,
            "watched_at": self.watched_at,
            "rating": self.rating,
            "source": self.source,
            "poster": self.poster,
            "genres": self.genres,
            "intent": self.intent,
            "seasons": [
                {
                    "number": season.number,
                    "watched_at": season.watched_at,
                    "episodes": [
                        {"number": e.number, "watched_at": e.watched_at, "ids": e.ids}
                        for e in season.episodes
                    ],
                }
                for season in (self.seasons[n] for n in sorted(self.seasons))
            ],
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "WatchItem":
        item = cls(
            title=raw["title"],
            media_type=raw.get("media_type", TV),
            year=raw.get("year"),
            ids=dict(raw.get("ids") or {}),
            status=raw.get("status"),
            watched_at=raw.get("watched_at"),
            rating=raw.get("rating"),
            source=raw.get("source", "file"),
            poster=raw.get("poster", ""),
            genres=list(raw.get("genres") or []),
            intent=raw.get("intent") if raw.get("intent") in VALID_INTENTS else WATCHED,
        )
        for season in raw.get("seasons") or []:
            number = int(season["number"])
            episodes = season.get("episodes") or []
            if not episodes:
                item.add_episode(number, None, season.get("watched_at"))
                continue
            for episode in episodes:
                item.add_episode(
                    number,
                    int(episode["number"]),
                    episode.get("watched_at"),
                    episode.get("ids") or {},
                )
        return item
