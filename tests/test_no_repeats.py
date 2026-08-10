"""Nothing you have already answered should ever be offered again.

Four separate records have to cover this between them, because none of them
survives every path on its own:

  library.json   what is on the Simkl account - but cached, so it can lag
  queue.json     accepted and not yet sent - but emptied by --send
  accepted.json  ever accepted - survives the send
  rejected.json  ever declined
"""

import json

import pytest

from simkl_importer.config import Store
from simkl_importer.models import TV, WatchItem


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "home")


def accepted_record(simkl, title="Severance"):
    return WatchItem(title=title, media_type=TV, ids={"simkl": simkl}, genres=["drama"]).to_dict()


# --------------------------------------------------------------- accepted ids


def test_accepted_ids_survive_the_queue_being_cleared(store):
    """The regression: accept -> send -> the title must not come back."""
    store.save_accepted([accepted_record(1153726)])
    store.save_queue([accepted_record(1153726)])

    store.clear_queue()  # what a successful --send does

    assert store.load_queue() == []
    assert store.accepted_ids() == {"1153726"}


def test_accepted_ids_are_strings_whatever_the_json_held(store):
    store.save_accepted([accepted_record(42), accepted_record("77")])
    assert store.accepted_ids() == {"42", "77"}


def test_accepted_ids_ignore_records_with_no_simkl_id(store):
    store.save_accepted([{"title": "Mystery", "ids": {}}, accepted_record(5)])
    assert store.accepted_ids() == {"5"}


def test_accepted_ids_empty_on_a_fresh_install(store):
    assert store.accepted_ids() == set()


# ------------------------------------------------------------- library cache


def test_invalidate_library_forces_a_refetch(store):
    store.save_library({"fetched_at": 1_700_000_000, "items": {"6162": "tv"}})
    assert store.load_library()["items"] == {"6162": "tv"}

    store.invalidate_library()

    assert store.load_library() == {}
    assert not store.library_path.exists()


def test_invalidate_library_is_safe_when_there_is_no_cache(store):
    store.invalidate_library()
    store.invalidate_library()
    assert store.load_library() == {}


# ------------------------------------------------------------------ filtering


def build_entry(simkl, title):
    return {
        "candidate": {"title": title, "year": 2022, "genres": ["Drama"], "ids": {"simkl_id": simkl}},
        "section": "tv",
        "genre": "drama",
    }


def filter_entries(entries, library, rejected, already_accepted, queued_keys):
    """The exclusion rule from collect_session, isolated for testing."""
    from simkl_importer.discovery import SECTION_TO_TYPE, candidate_to_item
    from simkl_importer.matching import candidate_ids

    kept, counts = [], {"library": 0, "no": 0, "yes": 0, "queued": 0}
    for entry in entries:
        simkl_id = str(candidate_ids(entry["candidate"]).get("simkl") or "")
        if simkl_id and simkl_id in library:
            counts["library"] += 1
            continue
        if simkl_id and simkl_id in rejected:
            counts["no"] += 1
            continue
        if simkl_id and simkl_id in already_accepted:
            counts["yes"] += 1
            continue
        item = candidate_to_item(entry["candidate"], SECTION_TO_TYPE.get(entry["section"], TV))
        if item is None or item.dedupe_key() in queued_keys:
            counts["queued"] += 1
            continue
        kept.append(entry)
    return kept, counts


def test_every_kind_of_already_answered_is_excluded():
    entries = [
        build_entry(1, "On The Account"),
        build_entry(2, "Said No"),
        build_entry(3, "Said Yes And Sent"),
        build_entry(4, "Still Queued"),
        build_entry(5, "Genuinely New"),
    ]
    queued = WatchItem(title="Still Queued", media_type=TV, ids={"simkl": 4})

    kept, counts = filter_entries(
        entries,
        library={"1": "tv"},
        rejected={"2": {"title": "Said No", "genres": []}},
        already_accepted={"3"},
        queued_keys={queued.dedupe_key()},
    )

    assert [e["candidate"]["title"] for e in kept] == ["Genuinely New"]
    assert counts == {"library": 1, "no": 1, "yes": 1, "queued": 1}


def test_a_title_answered_in_a_previous_session_is_still_excluded(store):
    """Full round trip through disk, the way a second run sees it."""
    store.save_accepted([accepted_record(1153726, "Severance")])
    store.save_rejected({"342994": {"title": "John Wick", "genres": ["action"]}})
    store.clear_queue()
    store.invalidate_library()

    fresh = Store(store.home)  # a brand new process
    kept, counts = filter_entries(
        [build_entry(1153726, "Severance"), build_entry(342994, "John Wick"), build_entry(9, "New Thing")],
        library=fresh.load_library().get("items", {}),
        rejected=fresh.load_rejected(),
        already_accepted=fresh.accepted_ids(),
        queued_keys=set(),
    )

    assert [e["candidate"]["title"] for e in kept] == ["New Thing"]
    assert counts["yes"] == 1 and counts["no"] == 1


def test_accepted_file_is_append_only_across_runs(store):
    store.save_accepted([accepted_record(1)])
    existing = store.load_accepted()
    existing.append(accepted_record(2))
    store.save_accepted(existing)

    assert store.accepted_ids() == {"1", "2"}
    assert len(json.loads(store.accepted_path.read_text())) == 2
