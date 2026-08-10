"""Build a taste profile from what you have already said yes (and no) to.

Round one is a flat list of popular titles per genre. Round two uses the
answers from round one: genres you accepted score positively, genres you
rejected score negatively, common genres are damped so distinctive ones carry
the signal, and candidates are shown best-match first.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .client import SimklClient, SimklError
from .config import Store
from .models import ANIME, MOVIE, TV, WatchItem

SUMMARY_SECTION = {TV: "tv", MOVIE: "movies", ANIME: "anime"}

# a rejected title says less than an accepted one, but it does say something
REJECT_WEIGHT = 0.6
# Tie-breakers only. Both must stay well under 1.0, which is what a perfect
# genre match scores, so neither can outrank actually matching your taste.
RATING_WEIGHT = 0.08
ERA_WEIGHT = 0.10


def normalize_genre(genre: Any) -> str:
    return str(genre).strip().lower()


def candidate_genres(candidate: Dict[str, Any]) -> List[str]:
    genres = candidate.get("genres")
    if not isinstance(genres, list):
        return []
    return [normalize_genre(g) for g in genres if str(g).strip()]


def candidate_rating(candidate: Dict[str, Any]) -> Optional[float]:
    ratings = candidate.get("ratings") or {}
    for source in ("simkl", "imdb"):
        value = (ratings.get(source) or {}).get("rating")
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def decade(year: Optional[int]) -> Optional[int]:
    if not year:
        return None
    try:
        return (int(year) // 10) * 10
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ enrichment


class GenreLookup:
    """Genres per Simkl ID, cached on disk so we only ever fetch them once."""

    def __init__(self, client: SimklClient, store: Store):
        self.client = client
        self.store = store
        self.cache: Dict[str, Any] = store.load_genre_cache()
        self._dirty = False

    def flush(self) -> None:
        if self._dirty:
            self.store.save_genre_cache(self.cache)
            self._dirty = False

    def remember(self, simkl_id: Any, genres: Sequence[str]) -> None:
        if not simkl_id or not genres:
            return
        key = str(simkl_id)
        if self.cache.get(key) != list(genres):
            self.cache[key] = list(genres)
            self._dirty = True

    def get(self, simkl_id: Any, media_type: str = TV) -> List[str]:
        if not simkl_id:
            return []
        key = str(simkl_id)
        if key in self.cache:
            return list(self.cache[key])

        section = SUMMARY_SECTION.get(media_type, "tv")
        try:
            summary = self.client.summary(section, key)
        except SimklError:
            summary = None
        genres = candidate_genres(summary or {})
        self.cache[key] = genres
        self._dirty = True
        return genres

    def enrich(self, items: Sequence[WatchItem], progress: bool = True, log=print) -> int:
        """Fill in genres for queued items that do not have them yet."""
        missing = [item for item in items if not item.genres and item.ids.get("simkl")]
        uncached = [item for item in missing if str(item.ids["simkl"]) not in self.cache]
        if missing and progress and uncached:
            log(f"  Looking up genres for {len(uncached)} title(s) you already queued...")

        filled = 0
        for index, item in enumerate(missing, start=1):
            item.genres = self.get(item.ids["simkl"], item.media_type)
            if item.genres:
                filled += 1
            if progress and uncached and index % 10 == 0:
                log(f"    {index}/{len(missing)}")
        self.flush()
        return filled


# --------------------------------------------------------------------- profile


class TasteProfile:
    """Genre / era weights learned from accepted and rejected titles."""

    def __init__(self):
        self.accepted_genres: Counter = Counter()
        self.rejected_genres: Counter = Counter()
        self.decades: Counter = Counter()
        self.accepted = 0
        self.rejected = 0

    # ------------------------------------------------------------- building

    def add_accepted(self, genres: Iterable[str], year: Optional[int] = None) -> None:
        genres = [normalize_genre(g) for g in genres if str(g).strip()]
        if genres:
            self.accepted_genres.update(genres)
        era = decade(year)
        if era:
            self.decades[era] += 1
        self.accepted += 1

    def add_rejected(self, genres: Iterable[str]) -> None:
        genres = [normalize_genre(g) for g in genres if str(g).strip()]
        if genres:
            self.rejected_genres.update(genres)
        self.rejected += 1

    @property
    def empty(self) -> bool:
        return not self.accepted_genres

    # -------------------------------------------------------------- scoring

    def genre_affinity(self) -> Dict[str, float]:
        """Net per-genre preference in roughly -1..1."""
        affinity: Dict[str, float] = {}
        total_accepted = max(1, sum(self.accepted_genres.values()))
        total_rejected = max(1, sum(self.rejected_genres.values()))
        for genre in set(self.accepted_genres) | set(self.rejected_genres):
            liked = self.accepted_genres.get(genre, 0) / total_accepted
            disliked = self.rejected_genres.get(genre, 0) / total_rejected
            affinity[genre] = liked - REJECT_WEIGHT * disliked
        return affinity

    def top_genres(self, count: int = 6) -> List[str]:
        affinity = self.genre_affinity()
        ranked = sorted(affinity.items(), key=lambda kv: kv[1], reverse=True)
        return [genre for genre, weight in ranked[:count] if weight > 0]

    def summary(self) -> str:
        if self.empty:
            return "  No taste profile yet - this round will be ordered by popularity."
        top = ", ".join(
            f"{genre} x{count}" for genre, count in self.accepted_genres.most_common(6)
        )
        line = f"  Taste profile from {self.accepted} accepted title(s): {top}"
        if self.decades:
            eras = ", ".join(f"{era}s" for era, _ in self.decades.most_common(3))
            line += f"\n  Mostly {eras}."
        if self.rejected:
            line += f"\n  Also learned from {self.rejected} title(s) you passed on."
        return line


def build_profile(
    accepted: Sequence[WatchItem],
    rejected_genres: Optional[Sequence[Sequence[str]]] = None,
) -> TasteProfile:
    profile = TasteProfile()
    for item in accepted:
        profile.add_accepted(item.genres, item.year)
    for genres in rejected_genres or []:
        profile.add_rejected(genres)
    return profile


# --------------------------------------------------------------------- ranking


def genre_idf(entries: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Damp ubiquitous genres so "drama" does not drown out "cyberpunk"."""
    document_count = max(1, len(entries))
    counts: Counter = Counter()
    for entry in entries:
        counts.update(set(candidate_genres(entry.get("candidate", {}))))
    return {
        genre: math.log(1 + document_count / (1 + count))
        for genre, count in counts.items()
    }


def match_fraction(candidate: Dict[str, Any], profile: TasteProfile) -> Optional[float]:
    """How well this title matches the profile, 0..1, on its own terms.

    Cosine similarity between the title's genres and your affinity weights.
    The averaging this replaced made 100% far too cheap: one genre in common
    with a broad profile scored the same as a title matching it exactly.
    Comparing the *shapes* fixes that - a lone "Adventure" cannot look like a
    perfect fit for someone whose profile is crime and thriller, and reaching
    100% means covering what you actually watch, not clipping one corner of it.

    None means we cannot say: no profile yet, or no genres on the title.
    """
    if profile.empty:
        return None
    genres = candidate_genres(candidate)
    if not genres:
        return None

    affinity = {g: w for g, w in profile.genre_affinity().items() if w > 0}
    if not affinity:
        return None

    overlap = sum(affinity.get(genre, 0.0) for genre in genres)
    if overlap <= 0:
        return 0.0

    profile_norm = math.sqrt(sum(weight * weight for weight in affinity.values()))
    title_norm = math.sqrt(len(genres))
    if not profile_norm or not title_norm:
        return None
    return max(0.0, min(1.0, overlap / (profile_norm * title_norm)))


def score_candidate(
    candidate: Dict[str, Any],
    profile: TasteProfile,
    idf: Dict[str, float],
) -> float:
    """Ordering score: the match, nudged by rating and era to break ties.

    The nudges are deliberately small - two titles that match you equally well
    should be separated by how good they are, but a better rating must never
    push a title above one that actually fits your taste.
    """
    match = match_fraction(candidate, profile)
    base = 0.0 if match is None else match

    # distinctive genres count for a little more than ubiquitous ones
    genres = candidate_genres(candidate)
    if genres and match:
        rarity = sum(idf.get(genre, 1.0) for genre in genres) / len(genres)
        base *= 1.0 + 0.10 * math.tanh(rarity - 1.0)

    rating = candidate_rating(candidate)
    rating_score = ((rating - 6.0) / 4.0) if rating else 0.0

    era_score = 0.0
    era = decade(candidate.get("year"))
    if era and profile.decades:
        top = max(profile.decades.values())
        era_score = profile.decades.get(era, 0) / top if top else 0.0

    return base + RATING_WEIGHT * rating_score + ERA_WEIGHT * era_score


def rank_entries(
    entries: List[Dict[str, Any]],
    profile: TasteProfile,
) -> List[Tuple[Dict[str, Any], float]]:
    """Sort candidates best-match first and attach a 0-100 match percentage."""
    if not entries:
        return []
    idf = genre_idf(entries)
    scored = [(entry, score_candidate(entry.get("candidate", {}), profile, idf)) for entry in entries]

    if profile.empty:
        # nothing learned yet: keep the API's popularity order
        return [(entry, 0.0) for entry, _ in scored]

    scored.sort(key=lambda pair: pair[1], reverse=True)
    # The percentage is the honest match, not the position: a pool of poor
    # candidates should read as a pool of poor candidates, not get stretched so
    # its least-bad member shows 100%.
    out = []
    for entry, _ in scored:
        match = match_fraction(entry.get("candidate", {}), profile)
        out.append((entry, 100.0 * match if match is not None else None))
    return out
