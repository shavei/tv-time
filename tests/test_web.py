"""Tests for the local web picker - server behaviour, not the browser."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from simkl_importer.models import MOVIE, TV, WatchItem
from simkl_importer.taste import TasteProfile, build_profile
from simkl_importer.web import DiscoverySession, Handler, candidate_payload
from http.server import ThreadingHTTPServer


def entry(title, simkl, genres=("Drama",), year=2015, poster="54/abc", section="tv"):
    return {
        "candidate": {
            "title": title,
            "year": year,
            "genres": list(genres),
            "poster": poster,
            "ids": {"simkl_id": simkl},
            "ratings": {"simkl": {"rating": 8.4}},
        },
        "section": section,
        "genre": genres[0].lower(),
    }


def ranked(*entries):
    return [(e, 100.0 - index * 10) for index, e in enumerate(entries)]


@pytest.fixture
def session():
    profile = build_profile([WatchItem(title="seed", genres=["drama"])])
    return DiscoverySession(ranked(entry("Severance", 1), entry("Heat", 2, section="movies")), profile, set())


# ------------------------------------------------------------------- payload


def test_payload_points_at_the_cdn_not_a_local_file(session):
    poster = session.payload[0]["poster"]
    assert poster.startswith("https://wsrv.nl/?url=https://simkl.in/posters/54/abc")
    assert "w=300" in poster


def test_payload_carries_what_the_page_needs(session):
    first = session.payload[0]
    assert first["title"] == "Severance"
    assert first["genres"] == ["Drama"]
    assert first["rating"] == 8.4
    assert first["match"] == 100
    assert first["url"].startswith("https://simkl.com/tv/")


def test_movies_are_flagged_so_the_page_skips_the_progress_box(session):
    movie = [c for c in session.payload if c["title"] == "Heat"][0]
    assert movie["is_movie"] is True
    assert movie["url"].startswith("https://simkl.com/movies/")


def test_match_is_hidden_when_nothing_is_known_yet():
    blank = DiscoverySession(ranked(entry("Severance", 1)), TasteProfile(), set())
    assert blank.payload[0]["match"] is None
    assert blank.state()["has_match"] is False


def test_candidates_are_deduplicated_by_simkl_id():
    duped = DiscoverySession(ranked(entry("A", 7), entry("A again", 7)), TasteProfile(), set())
    assert len(duped.payload) == 1


def test_candidate_without_a_poster_still_renders():
    item = WatchItem(title="No Art", media_type=TV, ids={"simkl": 3})
    payload = candidate_payload(entry("No Art", 3, poster=""), 50.0, item, True)
    assert payload["poster"] == ""


# -------------------------------------------------------------------- commit


def test_commit_applies_progress_and_records_rejections(session):
    result = session.commit({
        "accepted": [{"id": "1", "progress": "s1, s2e1-2"}],
        "rejected": ["2"],
    })

    assert result == {"ok": True, "queued": 1, "rejected": 1}
    assert session.finished.is_set()
    queued = session.accepted[0]
    assert queued.seasons[1].episodes == []
    assert sorted(e.number for e in queued.seasons[2].episodes) == [1, 2]
    assert session.rejected_ids == ["2"]


def test_commit_defaults_to_the_whole_show(session):
    session.commit({"accepted": [{"id": "1"}], "rejected": []})
    assert session.accepted[0].whole_thing


def test_commit_rejects_unparseable_progress_without_queueing_anything(session):
    result = session.commit({"accepted": [{"id": "1", "progress": "some of it"}], "rejected": []})

    assert result["ok"] is False
    assert "Severance" in result["errors"][0]
    assert session.accepted == []
    assert not session.finished.is_set()


def test_commit_ignores_ids_that_were_never_offered(session):
    result = session.commit({"accepted": [{"id": "999", "progress": "all"}], "rejected": ["999"]})
    assert result["queued"] == 0
    assert session.rejected_ids == []


def test_cancel_unblocks_the_caller(session):
    session.cancel()
    assert session.cancelled
    assert session.finished.is_set()


# -------------------------------------------------------------------- server


@pytest.fixture
def server(session):
    handler = type("Bound", (Handler,), {"session": session})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", session
    httpd.shutdown()
    httpd.server_close()


def get(url, **kwargs):
    return urllib.request.urlopen(urllib.request.Request(url, **kwargs), timeout=5)


def test_state_requires_the_token(server):
    base, session = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(f"{base}/api/state?token=wrong")
    assert excinfo.value.code == 403

    body = json.loads(get(f"{base}/api/state?token={session.token}").read())
    assert len(body["candidates"]) == 2


def test_page_is_served_with_a_valid_token(server):
    base, session = server
    html = get(f"{base}/?token={session.token}").read().decode()
    assert "<title>Simkl importer" in html
    assert "api/commit" in html


def test_non_loopback_host_header_is_refused(server):
    base, session = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(f"{base}/api/state?token={session.token}", headers={"Host": "evil.example.com"})
    assert excinfo.value.code == 403


def test_commit_over_http_finishes_the_session(server):
    base, session = server
    payload = json.dumps({"accepted": [{"id": "1", "progress": "all"}], "rejected": ["2"]}).encode()
    response = get(
        f"{base}/api/commit?token={session.token}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    assert json.loads(response.read())["ok"] is True
    assert session.finished.wait(timeout=2)
    assert session.accepted[0].title == "Severance"


def test_unknown_paths_404(server):
    base, session = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(f"{base}/wat?token={session.token}")
    assert excinfo.value.code == 404
