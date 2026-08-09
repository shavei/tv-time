"""Batch items into POST /sync/history calls and report what happened."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .client import SimklClient, SimklError
from .models import WatchItem

DEFAULT_BATCH_SIZE = 50


@dataclass
class SyncReport:
    added_movies: int = 0
    added_shows: int = 0
    added_episodes: int = 0
    batches_sent: int = 0
    batches_failed: int = 0
    not_found: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    statuses: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def total_added(self) -> int:
        return self.added_movies + self.added_shows + self.added_episodes

    def summary(self) -> str:
        lines = [
            "",
            "=" * 60,
            "Import summary",
            "=" * 60,
            f"  Batches sent      : {self.batches_sent}"
            + (f"  ({self.batches_failed} failed)" if self.batches_failed else ""),
            f"  Movies added      : {self.added_movies}",
            f"  Shows added       : {self.added_shows}",
            f"  Episodes added    : {self.added_episodes}",
            f"  Not found by Simkl: {len(self.not_found)}",
        ]
        if self.errors:
            lines.append(f"  Errors            : {len(self.errors)}")
            for error in self.errors[:5]:
                lines.append(f"      - {error}")
            if len(self.errors) > 5:
                lines.append(f"      ... and {len(self.errors) - 5} more")
        return "\n".join(lines)


def build_batches(items: Sequence[WatchItem], batch_size: int = DEFAULT_BATCH_SIZE) -> List[Dict[str, Any]]:
    """Group items into /sync/history payloads of at most `batch_size` items."""
    batches: List[Dict[str, Any]] = []
    current: Dict[str, List[Dict[str, Any]]] = {"movies": [], "shows": []}
    count = 0

    for item in items:
        current[item.bucket].append(item.to_payload())
        count += 1
        if count >= batch_size:
            batches.append({key: value for key, value in current.items() if value})
            current = {"movies": [], "shows": []}
            count = 0

    if count:
        batches.append({key: value for key, value in current.items() if value})
    return batches


def _describe_not_found(entry: Any) -> str:
    if isinstance(entry, dict):
        title = entry.get("title") or entry.get("ids", {}).get("simkl") or "?"
        year = entry.get("year")
        return f"{title}{f' ({year})' if year else ''}"
    return str(entry)


def push_history(
    client: SimklClient,
    items: Sequence[WatchItem],
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> SyncReport:
    report = SyncReport()
    batches = build_batches(items, batch_size)
    if not batches:
        print("  Nothing to send.")
        return report

    total_items = len(items)
    print(
        f"\nSending {total_items} item(s) in {len(batches)} batch(es) of up to {batch_size}."
        + ("  [DRY RUN - nothing will be written]" if dry_run else "")
    )

    sent = 0
    for index, payload in enumerate(batches, start=1):
        size = sum(len(value) for value in payload.values())
        sent += size
        prefix = f"  [{index}/{len(batches)}] {size} item(s)"
        if dry_run:
            print(f"{prefix} - would POST /sync/history")
            report.batches_sent += 1
            continue

        print(f"{prefix} - POST /sync/history ... ", end="", flush=True)
        try:
            response = client.add_to_history(payload)
        except SimklError as exc:
            report.batches_failed += 1
            report.errors.append(f"batch {index}: {exc}")
            print("FAILED")
            print(f"      {exc}")
            continue

        report.batches_sent += 1
        added = response.get("added") or {}
        report.added_movies += int(added.get("movies") or 0)
        report.added_shows += int(added.get("shows") or 0)
        report.added_episodes += int(added.get("episodes") or 0)
        for status in added.get("statuses") or []:
            report.statuses.append(status)

        not_found = response.get("not_found") or {}
        for bucket in ("movies", "shows", "episodes"):
            for entry in not_found.get(bucket) or []:
                report.not_found.append({"bucket": bucket, "entry": entry})

        print(
            f"ok (+{added.get('movies', 0)} movies, "
            f"+{added.get('shows', 0)} shows, +{added.get('episodes', 0)} episodes)"
        )
        print(f"      progress: {sent}/{total_items} items")

    if report.not_found:
        print(f"\n  Simkl could not match {len(report.not_found)} item(s):")
        for record in report.not_found[:15]:
            print(f"      - [{record['bucket']}] {_describe_not_found(record['entry'])}")
        if len(report.not_found) > 15:
            print(f"      ... and {len(report.not_found) - 15} more")

    return report


def write_unmatched_csv(path: Path, items: Sequence[WatchItem], report: Optional[SyncReport] = None) -> Optional[Path]:
    """Save everything we could not import so it can be fixed up and re-run."""
    rows = [
        {
            "title": item.title,
            "year": item.year or "",
            "type": item.media_type,
            "progress": item.describe_progress(),
            "reason": item.match_note or "no match",
        }
        for item in items
    ]
    if report:
        for record in report.not_found:
            entry = record["entry"]
            rows.append(
                {
                    "title": entry.get("title", "") if isinstance(entry, dict) else str(entry),
                    "year": entry.get("year", "") if isinstance(entry, dict) else "",
                    "type": record["bucket"],
                    "progress": "",
                    "reason": "rejected by /sync/history (not_found)",
                }
            )
    if not rows:
        return None

    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["title", "year", "type", "progress", "reason"])
        writer.writeheader()
        writer.writerows(rows)
    return path
