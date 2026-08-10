"""Tests for the browser flow - server behaviour, not the page itself."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from simkl_importer.config import Store
from simkl_importer.models import TV, WatchItem
from simkl_importer.taste import TasteProfile, build_profile
from simkl_importer.web import (
    DiscoverySession,
    Handler,
    WebFlow,
    candidate_payload,
    open_in_browser,
)


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
    return DiscoverySession(
        ranked(entry("Severance", 1), entry("Heat", 2, section="movies")), profile, set()
    )


@pytest.fixture
def flow(tmp_path, session):
    """A WebFlow already holding a built session, as if the build had finished."""
    flow = WebFlow(client=None, store=Store(tmp_path / "home"), queue=[])
    flow.session = session
    flow.session_data = {
        "ranked": [],
        "profile": session.profile,
        "rejected": {},
        "queued_keys": set(),
        "skipped": 3,
        "skip_reasons": ["3 already on your account"],
    }
    return flow


# -------------------------------------------------------------------- payload


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


def test_candidates_are_deduplicated_by_simkl_id():
    duped = DiscoverySession(ranked(entry("A", 7), entry("A again", 7)), TasteProfile(), set())
    assert len(duped.payload) == 1


def test_candidate_without_a_poster_still_renders():
    item = WatchItem(title="No Art", media_type=TV, ids={"simkl": 3})
    assert candidate_payload(entry("No Art", 3, poster=""), 50.0, item, True)["poster"] == ""


# --------------------------------------------------------------------- commit


def test_commit_applies_progress_and_records_rejections(flow):
    result = flow.commit({"accepted": [{"id": "1", "progress": "s1, s2e1-2"}], "rejected": ["2"]})

    assert result == {"ok": True, "queued": 1, "rejected": 1}
    assert flow.finished.is_set()
    queued = flow.session.accepted[0]
    assert queued.seasons[1].episodes == []
    assert sorted(e.number for e in queued.seasons[2].episodes) == [1, 2]
    assert flow.session.rejected_ids == ["2"]


def test_commit_defaults_to_the_whole_show(flow):
    flow.commit({"accepted": [{"id": "1"}], "rejected": []})
    assert flow.session.accepted[0].whole_thing


def test_commit_rejects_unparseable_progress_without_queueing_anything(flow):
    result = flow.commit({"accepted": [{"id": "1", "progress": "some of it"}], "rejected": []})

    assert result["ok"] is False
    assert "Severance" in result["errors"][0]
    assert flow.session.accepted == []
    assert not flow.finished.is_set()


def test_commit_ignores_ids_that_were_never_offered(flow):
    result = flow.commit({"accepted": [{"id": "999", "progress": "all"}], "rejected": ["999"]})
    assert result["queued"] == 0
    assert flow.session.rejected_ids == []


def test_commit_before_the_build_finishes_is_refused(tmp_path):
    flow = WebFlow(client=None, store=Store(tmp_path / "home"), queue=[])
    assert flow.commit({"accepted": []})["ok"] is False
    assert not flow.finished.is_set()


def test_cancel_unblocks_the_caller(flow):
    flow.cancel()
    assert flow.cancelled
    assert flow.finished.is_set()


# ---------------------------------------------------------------------- phases


def test_prep_state_is_not_ready_until_the_thread_lands(tmp_path):
    flow = WebFlow(client=None, store=Store(tmp_path / "home"), queue=[])
    state = flow.prep_state()

    assert state["ready"] is False
    assert state["suggested"] == []
    assert "drama" in state["all_genres"]
    assert set(state["sections"]) == {"tv", "movies", "anime"}


def test_prep_failure_is_reported_rather_than_swallowed(tmp_path, monkeypatch):
    import simkl_importer.discovery as discovery

    def boom(*args, **kwargs):
        raise RuntimeError("account unreachable")

    monkeypatch.setattr(discovery, "prepare_taste", boom)
    flow = WebFlow(client=None, store=Store(tmp_path / "home"), queue=[])
    flow._prep()

    state = flow.prep_state()
    assert state["ready"] is True
    assert "account unreachable" in state["error"]
    assert any("account unreachable" in line for line in state["log"])


def test_build_cannot_start_before_prep_lands(tmp_path):
    flow = WebFlow(client=None, store=Store(tmp_path / "home"), queue=[])
    result = flow.start_build({"sections": ["tv"]})

    assert result["ok"] is False
    assert "one moment" in result["error"]


def test_build_failure_surfaces_in_build_state(tmp_path, monkeypatch):
    import simkl_importer.discovery as discovery

    def boom(*args, **kwargs):
        raise RuntimeError("genre endpoint down")

    monkeypatch.setattr(discovery, "build_session", boom)
    flow = WebFlow(client=None, store=Store(tmp_path / "home"), queue=[])
    flow.prepared = {"library": {}, "profile": TasteProfile(), "suggested": []}
    flow._build({"sections": ["tv"]})

    state = flow.build_state()
    assert state["ready"] is True
    assert "genre endpoint down" in state["error"]
    assert state["candidates"] == []


def test_build_state_reports_what_was_skipped(flow):
    state = flow.build_state()
    assert state["ready"] is True
    assert state["skipped"] == 3
    assert state["skip_reasons"] == ["3 already on your account"]
    assert len(state["candidates"]) == 2


def test_log_lines_are_trimmed_and_blank_ones_dropped(tmp_path):
    flow = WebFlow(client=None, store=Store(tmp_path / "home"), queue=[])
    flow.log("  indented  ")
    flow.log("")
    flow.log("   ")
    assert flow.log_lines() == ["indented"]


# --------------------------------------------------------------------- server


@pytest.fixture
def server(flow):
    handler = type("Bound", (Handler,), {"flow": flow})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", flow
    httpd.shutdown()
    httpd.server_close()


def get(url, **kwargs):
    return urllib.request.urlopen(urllib.request.Request(url, **kwargs), timeout=5)


def test_endpoints_require_the_token(server):
    base, flow = server
    for path in ("/api/prep", "/api/build"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            get(f"{base}{path}?token=wrong")
        assert excinfo.value.code == 403

    body = json.loads(get(f"{base}/api/build?token={flow.token}").read())
    assert len(body["candidates"]) == 2


def test_page_is_served_with_a_valid_token(server):
    base, flow = server
    html = get(f"{base}/?token={flow.token}").read().decode()
    assert "<title>Simkl importer" in html
    assert "api/prep" in html and "api/start" in html and "api/commit" in html


def test_page_without_a_token_explains_itself(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(f"{base}/")
    assert excinfo.value.code == 403


def test_non_loopback_host_header_is_refused(server):
    base, flow = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(f"{base}/api/prep?token={flow.token}", headers={"Host": "evil.example.com"})
    assert excinfo.value.code == 403


def test_commit_over_http_finishes_the_flow(server):
    base, flow = server
    payload = json.dumps({"accepted": [{"id": "1", "progress": "all"}], "rejected": ["2"]}).encode()
    response = get(
        f"{base}/api/commit?token={flow.token}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    assert json.loads(response.read())["ok"] is True
    assert flow.finished.wait(timeout=2)
    assert flow.session.accepted[0].title == "Severance"


def test_cancel_over_http_finishes_the_flow(server):
    base, flow = server
    get(f"{base}/api/cancel?token={flow.token}", data=b"{}",
        headers={"Content-Type": "application/json"})
    assert flow.finished.wait(timeout=2)
    assert flow.cancelled


def test_malformed_json_is_rejected(server):
    base, flow = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(f"{base}/api/commit?token={flow.token}", data=b"not json",
            headers={"Content-Type": "application/json"})
    assert excinfo.value.code == 400


def test_unknown_paths_404(server):
    base, flow = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(f"{base}/wat?token={flow.token}")
    assert excinfo.value.code == 404


# ------------------------------------------------------------------- launcher


def test_open_in_browser_uses_webbrowser_when_it_works(monkeypatch):
    calls = []
    monkeypatch.setattr("simkl_importer.web.webbrowser.open", lambda url: calls.append(url) or True)
    assert open_in_browser("http://127.0.0.1:1/") is True
    assert calls == ["http://127.0.0.1:1/"]


def test_open_in_browser_falls_through_when_webbrowser_returns_false(monkeypatch):
    """webbrowser.open() returns False rather than raising - the old blind spot."""
    monkeypatch.setattr("simkl_importer.web.webbrowser.open", lambda url: False)
    monkeypatch.setattr("simkl_importer.web.sys.platform", "linux")
    spawned = []
    monkeypatch.setattr("simkl_importer.web.subprocess.Popen",
                        lambda cmd, **kw: spawned.append(cmd))
    assert open_in_browser("http://127.0.0.1:1/") is True
    assert spawned and spawned[0][0] == "xdg-open"


def test_open_in_browser_reports_failure_instead_of_pretending(monkeypatch):
    monkeypatch.setattr("simkl_importer.web.webbrowser.open", lambda url: False)
    monkeypatch.setattr("simkl_importer.web.sys.platform", "linux")

    def boom(*args, **kwargs):
        raise OSError("no opener")

    monkeypatch.setattr("simkl_importer.web.subprocess.Popen", boom)
    assert open_in_browser("http://127.0.0.1:1/") is False


def test_open_in_browser_survives_webbrowser_raising(monkeypatch):
    def boom(url):
        raise RuntimeError("no display")

    monkeypatch.setattr("simkl_importer.web.webbrowser.open", boom)
    monkeypatch.setattr("simkl_importer.web.sys.platform", "darwin")
    spawned = []
    monkeypatch.setattr("simkl_importer.web.subprocess.Popen",
                        lambda cmd, **kw: spawned.append(cmd))
    assert open_in_browser("http://127.0.0.1:1/") is True
    assert spawned[0][0] == "open"
