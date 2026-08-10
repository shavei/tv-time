"""Offline tests - none of these touch the network."""

from pathlib import Path

import pytest

from simkl_importer.models import MOVIE, TV, WatchItem
from simkl_importer.parsers import (
    load_file,
    map_headers,
    split_title_and_episode,
    to_utc_iso,
    to_year,
)
from simkl_importer.progress import ProgressError, apply_progress, parse_progress
from simkl_importer.sync import build_batches

SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"


# --------------------------------------------------------------------- values


def test_header_aliases_map_to_canonical_fields():
    mapping = map_headers(["Show Name", "Release Year", "Season Number", "Ep", "IMDB ID"])
    assert set(mapping.values()) == {"title", "year", "season", "episode", "imdb"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2013", 2013),
        ("2013-04-07", 2013),
        ("April 2019", 2019),
        ("", None),
    ],
)
def test_to_year(raw, expected):
    assert to_year(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2011-09-01T14:44:00Z", "2011-09-01T14:44:00Z"),
        ("2011-09-01 14:44:00", "2011-09-01T14:44:00Z"),
        ("2011/09/01", "2011-09-01T00:00:00Z"),
        ("nonsense", None),
    ],
)
def test_to_utc_iso(raw, expected):
    assert to_utc_iso(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The Wire - S02E05", ("The Wire", 2, 5)),
        ("Lost 3x08", ("Lost", 3, 8)),
        ("Arrival", ("Arrival", None, None)),
    ],
)
def test_split_title_and_episode(raw, expected):
    assert split_title_and_episode(raw) == expected


# ---------------------------------------------------------------- file import


def test_csv_collapses_episode_rows_into_one_show():
    items = load_file(SAMPLES / "watched.csv")
    by_title = {item.title: item for item in items}

    walking_dead = by_title["The Walking Dead"]
    assert walking_dead.media_type == TV
    assert sorted(walking_dead.seasons) == [1, 2]
    assert sorted(e.number for e in walking_dead.seasons[1].episodes) == [1, 2, 3]
    assert walking_dead.ids["imdb"] == "tt1520211"

    # a row with no season/episode means "all of it"
    assert by_title["Breaking Bad"].whole_thing
    assert by_title["Breaking Bad"].to_payload()["status"] == "completed"

    # a row with a season but no episode means the whole season
    assert by_title["Severance"].seasons[1].episodes == []

    assert by_title["John Wick"].media_type == MOVIE


def test_json_import_handles_nested_ids():
    items = load_file(SAMPLES / "watched.json")
    by_title = {item.title: item for item in items}

    wire = by_title["The Wire"]
    assert sorted(e.number for e in wire.seasons[1].episodes) == [1, 2]
    assert wire.ids["imdb"] == "tt0306414"

    arrival = by_title["Arrival"]
    assert arrival.media_type == MOVIE
    assert arrival.rating == 9


# ------------------------------------------------------------------- payloads


def test_show_payload_shape_matches_simkl_docs():
    item = WatchItem(title="The Walking Dead", year=2010, ids={"simkl": 2090})
    item.add_episode(1, None, "2011-09-01T14:44:00Z")
    item.add_episode(2, 1)
    item.add_episode(2, 2)

    payload = item.to_payload()
    assert payload["title"] == "The Walking Dead"
    assert payload["ids"] == {"simkl": 2090}
    assert payload["seasons"][0] == {"number": 1, "watched_at": "2011-09-01T14:44:00Z"}
    assert payload["seasons"][1]["episodes"] == [{"number": 1}, {"number": 2}]


def test_partial_show_does_not_carry_a_top_level_watched_at():
    """A show-level watched_at would mark the whole title, not just the parts."""
    item = WatchItem(title="Lost", watched_at="2010-01-01T00:00:00Z")
    item.add_episode(1, 1, "2010-01-01T00:00:00Z")
    assert "watched_at" not in item.to_payload()

    whole = WatchItem(title="Lost", watched_at="2010-01-01T00:00:00Z")
    assert whole.to_payload()["watched_at"] == "2010-01-01T00:00:00Z"


def test_unknown_id_keys_are_dropped():
    item = WatchItem(title="X", ids={"simkl": 1, "tvtime": "abc"})
    assert item.to_payload()["ids"] == {"simkl": 1}


def test_batches_split_by_size_and_bucket():
    items = [WatchItem(title=f"Show {n}", ids={"simkl": n}) for n in range(3)]
    items += [WatchItem(title=f"Film {n}", media_type=MOVIE, ids={"simkl": 100 + n}) for n in range(2)]

    batches = build_batches(items, batch_size=2)
    assert len(batches) == 3
    assert sum(len(v) for batch in batches for v in batch.values()) == 5
    assert "shows" in batches[0]
    assert "movies" in batches[-1]


def test_merge_folds_duplicate_records():
    first = WatchItem(title="Lost", year=2004)
    first.add_episode(1, 1)
    second = WatchItem(title="Lost", year=2004, ids={"imdb": "tt0411008"})
    second.add_episode(1, 2)
    first.merge(second)

    assert first.ids["imdb"] == "tt0411008"
    assert sorted(e.number for e in first.seasons[1].episodes) == [1, 2]


# ------------------------------------------------------------------- progress


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("all", None),
        ("", None),
        ("s1", [(1, None)]),
        ("2", [(2, None)]),
        ("1-3", [(1, None), (2, None), (3, None)]),
        ("s2e5", [(2, 5)]),
        ("2x05", [(2, 5)]),
        ("s2e1-3", [(2, 1), (2, 2), (2, 3)]),
        ("s1, s2e1-2", [(1, None), (2, 1), (2, 2)]),
    ],
)
def test_parse_progress(spec, expected):
    assert parse_progress(spec) == expected


def test_parse_progress_rejects_junk():
    with pytest.raises(ProgressError):
        parse_progress("half of it maybe")


def test_apply_progress_all_clears_seasons():
    item = WatchItem(title="Lost")
    item.add_episode(1, 1)
    apply_progress(item, parse_progress("all"))
    assert item.whole_thing
    assert item.status == "completed"


def test_apply_progress_partial():
    item = WatchItem(title="Lost")
    apply_progress(item, parse_progress("s1, s2e1-2"))
    assert item.seasons[1].episodes == []
    assert sorted(e.number for e in item.seasons[2].episodes) == [1, 2]


def test_roundtrip_serialisation():
    item = WatchItem(title="Lost", year=2004, ids={"simkl": 42})
    item.add_episode(1, 1, "2010-01-01T00:00:00Z")
    item.add_episode(2, None)

    restored = WatchItem.from_dict(item.to_dict())
    assert restored.to_payload() == item.to_payload()


# ------------------------------------------------------------- plan to watch


def test_plan_items_render_the_add_to_list_shape():
    from simkl_importer.models import PLAN

    item = WatchItem(title="Dune", media_type=MOVIE, year=2021,
                     ids={"simkl": 1, "imdb": "tt1160419"}, intent=PLAN)
    payload = item.to_list_payload()

    assert payload == {
        "title": "Dune",
        "to": "plantowatch",
        "year": 2021,
        "ids": {"simkl": 1, "imdb": "tt1160419"},
    }
    assert item.describe_progress() == "plan to watch"


def test_intent_survives_a_round_trip_through_the_queue():
    from simkl_importer.models import PLAN

    item = WatchItem(title="Dune", ids={"simkl": 1}, intent=PLAN)
    assert WatchItem.from_dict(item.to_dict()).is_plan


def test_unknown_intent_falls_back_to_watched():
    restored = WatchItem.from_dict({"title": "X", "intent": "nonsense"})
    assert restored.is_plan is False


def test_merging_a_watched_record_over_a_planned_one_wins():
    from simkl_importer.models import PLAN, WATCHED

    planned = WatchItem(title="Dune", ids={"simkl": 1}, intent=PLAN)
    planned.merge(WatchItem(title="Dune", ids={"simkl": 1}, intent=WATCHED))
    assert planned.is_plan is False


def test_queue_splits_by_intent():
    from simkl_importer.models import PLAN
    from simkl_importer.sync import split_by_intent

    items = [
        WatchItem(title="Seen It", ids={"simkl": 1}),
        WatchItem(title="Later", ids={"simkl": 2}, intent=PLAN),
        WatchItem(title="Also Seen", ids={"simkl": 3}),
    ]
    watched, planned = split_by_intent(items)

    assert [i.title for i in watched] == ["Seen It", "Also Seen"]
    assert [i.title for i in planned] == ["Later"]


def test_list_batches_use_the_add_to_list_shape():
    from simkl_importer.models import PLAN
    from simkl_importer.sync import build_batches

    items = [
        WatchItem(title="Later Show", ids={"simkl": 1}, intent=PLAN),
        WatchItem(title="Later Film", media_type=MOVIE, ids={"simkl": 2}, intent=PLAN),
    ]
    batch = build_batches(items, batch_size=50, for_list=True)[0]

    assert batch["shows"][0]["to"] == "plantowatch"
    assert batch["movies"][0]["to"] == "plantowatch"
    assert "seasons" not in batch["shows"][0] and "status" not in batch["shows"][0]


def test_a_plan_to_watch_status_column_queues_rather_than_marks_watched():
    from simkl_importer.parsers import normalize_row, map_headers, row_to_item

    for label in ("plantowatch", "plan to watch", "watchlist", "watch later"):
        raw = {"title": "Dune", "year": "2021", "type": "movie", "status": label}
        item = row_to_item(normalize_row(raw, map_headers(raw.keys())))
        assert item.is_plan, label
        assert item.to_list_payload()["to"] == "plantowatch"


def test_an_ordinary_status_still_means_watched():
    from simkl_importer.parsers import normalize_row, map_headers, row_to_item

    raw = {"title": "Dune", "year": "2021", "type": "movie", "status": "completed"}
    assert row_to_item(normalize_row(raw, map_headers(raw.keys()))).is_plan is False
