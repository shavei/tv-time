"""Parse the short "how much did you watch?" answers typed in discovery mode.

Accepted, comma separated:
    all                  -> the whole show
    s1        / 1        -> all of season 1
    s1-s3     / 1-3      -> all of seasons 1 to 3
    s2e5      / 2x05     -> season 2, episode 5 only
    s2e1-10              -> season 2, episodes 1 to 10
    s1, s2e1-4           -> combinations
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# (season, episode|None); episode None means "the whole season"
Segment = Tuple[int, Optional[int]]

WHOLE_WORDS = {"all", "a", "everything", "complete", "completed", "whole", "*"}

SEASON_RANGE = re.compile(r"^s?(\d{1,3})\s*-\s*s?(\d{1,3})$", re.I)
SEASON_ONLY = re.compile(r"^s?(\d{1,3})$", re.I)
EPISODE_RANGE = re.compile(r"^s?(\d{1,3})\s*(?:[ex]|x)\s*(\d{1,4})\s*-\s*(?:e)?(\d{1,4})$", re.I)
EPISODE_ONE = re.compile(r"^s?(\d{1,3})\s*(?:[ex]|x)\s*(\d{1,4})$", re.I)

MAX_EPISODES_IN_RANGE = 500  # guard against a typo like s1e1-9999


class ProgressError(ValueError):
    pass


def parse_progress(text: str) -> Optional[List[Segment]]:
    """Return None for "the whole thing", else a list of segments.

    Raises ProgressError on input we cannot make sense of, so the caller can
    re-prompt instead of silently importing the wrong thing.
    """
    cleaned = (text or "").strip().lower()
    if not cleaned or cleaned in WHOLE_WORDS:
        return None

    segments: List[Segment] = []
    for chunk in re.split(r"[,;]+", cleaned):
        chunk = chunk.strip().replace(" ", "")
        if not chunk:
            continue
        if chunk in WHOLE_WORDS:
            return None

        match = EPISODE_RANGE.match(chunk)
        if match:
            season, first, last = (int(g) for g in match.groups())
            if last < first:
                first, last = last, first
            if last - first > MAX_EPISODES_IN_RANGE:
                raise ProgressError(f"'{chunk}' covers too many episodes")
            segments.extend((season, episode) for episode in range(first, last + 1))
            continue

        match = EPISODE_ONE.match(chunk)
        if match:
            segments.append((int(match.group(1)), int(match.group(2))))
            continue

        match = SEASON_RANGE.match(chunk)
        if match:
            first, last = int(match.group(1)), int(match.group(2))
            if last < first:
                first, last = last, first
            segments.extend((season, None) for season in range(first, last + 1))
            continue

        match = SEASON_ONLY.match(chunk)
        if match:
            segments.append((int(match.group(1)), None))
            continue

        raise ProgressError(f"could not read '{chunk}'")

    if not segments:
        raise ProgressError("nothing recognised")
    return segments


def apply_progress(item, segments: Optional[List[Segment]], watched_at: Optional[str] = None) -> None:
    """Write parsed segments onto a WatchItem."""
    if segments is None:
        item.seasons.clear()
        item.status = "completed"
        return
    for season, episode in segments:
        item.add_episode(season, episode, watched_at)
