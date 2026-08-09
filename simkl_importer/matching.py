"""Resolve titles to Simkl IDs before we post them.

Simkl will happily match on title+year alone, but resolving up front means:
  * unmatched titles are reported *before* anything is written to the account,
  * every item we do send carries a simkl_id, so matching is unambiguous, and
  * results are cached on disk, so a re-run costs almost no API quota.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional, Tuple

from .client import SimklClient, SimklError
from .config import Store
from .models import ANIME, MOVIE, TV, WatchItem, normalize_title

SEARCH_TYPE = {TV: "tv", MOVIE: "movie", ANIME: "anime"}
# if the first search comes up empty, try the neighbouring type
FALLBACK_TYPE = {TV: ANIME, ANIME: TV, MOVIE: TV}

# below this title similarity we treat a hit as "not really the same thing"
MIN_SCORE = 0.62


def candidate_ids(candidate: Dict[str, Any]) -> Dict[str, Any]:
    raw = candidate.get("ids") or {}
    ids: Dict[str, Any] = {}
    if raw.get("simkl_id") or raw.get("simkl"):
        ids["simkl"] = raw.get("simkl_id") or raw.get("simkl")
    for key in ("imdb", "tmdb", "tvdb", "mal", "anidb", "slug"):
        if raw.get(key):
            ids[key] = raw[key]
    return ids


def score_candidate(item: WatchItem, candidate: Dict[str, Any]) -> float:
    wanted = normalize_title(item.title)
    got = normalize_title(str(candidate.get("title") or ""))
    if not got:
        return 0.0
    score = difflib.SequenceMatcher(None, wanted, got).ratio()
    if wanted == got:
        score = 1.0

    candidate_year = candidate.get("year")
    if item.year and candidate_year:
        gap = abs(int(candidate_year) - int(item.year))
        if gap == 0:
            score += 0.25
        elif gap == 1:
            score += 0.10
        else:
            score -= 0.20
    return score


def pick_best(item: WatchItem, candidates: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], float]:
    best, best_score = None, 0.0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        score = score_candidate(item, candidate)
        if score > best_score:
            best, best_score = candidate, score
    return best, best_score


class Matcher:
    def __init__(self, client: SimklClient, store: Store, interactive: bool = False):
        self.client = client
        self.store = store
        self.interactive = interactive
        self.cache: Dict[str, Any] = store.load_cache()
        self._dirty = False

    def cache_key(self, item: WatchItem) -> str:
        return f"{item.media_type}|{normalize_title(item.title)}|{item.year or ''}"

    def flush(self) -> None:
        if self._dirty:
            self.store.save_cache(self.cache)
            self._dirty = False

    # ------------------------------------------------------------------ core

    def resolve(self, item: WatchItem) -> bool:
        """Fill in item.ids. Returns True when we are confident of the match."""
        if item.ids.get("simkl"):
            item.matched, item.match_note = True, "simkl id supplied"
            return True

        if item.ids.get("imdb"):
            found = self._by_imdb(str(item.ids["imdb"]))
            if found:
                item.ids.update(found)
                item.matched, item.match_note = True, "matched by imdb id"
                return True
            # fall through to a text search rather than giving up

        key = self.cache_key(item)
        if key in self.cache:
            cached = self.cache[key]
            if cached is None:
                item.matched, item.match_note = False, "no match (cached)"
                return False
            item.ids.update(cached)
            item.matched, item.match_note = True, "cached match"
            return True

        best, score, note = self._search(item)
        if best is None or score < MIN_SCORE:
            if self.interactive and best is not None:
                if self._confirm(item, best, score):
                    ids = candidate_ids(best)
                    item.ids.update(ids)
                    item.matched, item.match_note = True, "confirmed manually"
                    self.cache[key] = ids
                    self._dirty = True
                    return True
            self.cache[key] = None
            self._dirty = True
            item.matched = False
            item.match_note = note or f"best guess too weak (score {score:.2f})"
            return False

        ids = candidate_ids(best)
        if not ids.get("simkl"):
            item.matched, item.match_note = False, "search hit had no simkl id"
            return False

        item.ids.update(ids)
        if not item.year and best.get("year"):
            item.year = best["year"]
        item.matched = True
        item.match_note = f"{best.get('title')} ({best.get('year')}) score {score:.2f}"
        self.cache[key] = ids
        self._dirty = True
        return True

    def resolve_all(self, items: List[WatchItem], progress: bool = True) -> Tuple[List[WatchItem], List[WatchItem]]:
        matched: List[WatchItem] = []
        unmatched: List[WatchItem] = []
        total = len(items)
        for index, item in enumerate(items, start=1):
            if progress:
                print(f"  [{index}/{total}] {item.label():<45.45} ", end="", flush=True)
            try:
                ok = self.resolve(item)
            except SimklError as exc:
                ok = False
                item.matched, item.match_note = False, str(exc)
            if progress:
                print("ok" if ok else f"UNMATCHED - {item.match_note}")
            (matched if ok else unmatched).append(item)
        self.flush()
        return matched, unmatched

    # --------------------------------------------------------------- helpers

    def _by_imdb(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        try:
            results = self.client.search_by_imdb(imdb_id)
        except SimklError:
            return None
        for result in results:
            ids = candidate_ids(result)
            if ids.get("simkl"):
                return ids
        return None

    def _search(self, item: WatchItem) -> Tuple[Optional[Dict[str, Any]], float, str]:
        query = item.title if not item.year else f"{item.title} {item.year}"
        overall_best: Optional[Dict[str, Any]] = None
        overall_score = 0.0
        tried: List[str] = []

        for media_type in (item.media_type, FALLBACK_TYPE.get(item.media_type)):
            if media_type is None or media_type in tried:
                continue
            tried.append(media_type)
            try:
                results = self.client.search(SEARCH_TYPE[media_type], query, limit=8)
                if not results and item.year:
                    # the year in the query can hurt shows with odd metadata
                    results = self.client.search(SEARCH_TYPE[media_type], item.title, limit=8)
            except SimklError as exc:
                return overall_best, overall_score, f"search failed: {exc}"

            best, score = pick_best(item, results)
            if score > overall_score:
                overall_best, overall_score = best, score
            if best is not None and score >= MIN_SCORE:
                if media_type != item.media_type:
                    item.media_type = media_type
                return best, score, ""

        note = "no search results" if overall_best is None else ""
        return overall_best, overall_score, note

    def _confirm(self, item: WatchItem, candidate: Dict[str, Any], score: float) -> bool:
        print()
        print(f"    Unsure about '{item.label()}'.")
        print(f"    Closest hit: {candidate.get('title')} ({candidate.get('year')}) - score {score:.2f}")
        answer = input("    Use it? [y/N]: ").strip().lower()
        return answer.startswith("y")
