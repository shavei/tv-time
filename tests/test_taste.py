"""Offline tests for the taste profile and poster rendering."""

import io

import pytest

from simkl_importer.images import DEFAULT_WIDTH, PosterRenderer, poster_url
from simkl_importer.models import MOVIE, TV, WatchItem
from simkl_importer.taste import (
    TasteProfile,
    build_profile,
    candidate_genres,
    decade,
    genre_idf,
    rank_entries,
    score_candidate,
)

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def entry(title, genres, year=2015, rating=None, simkl=None):
    candidate = {
        "title": title,
        "year": year,
        "genres": genres,
        "ids": {"simkl_id": simkl or abs(hash(title)) % 100000},
    }
    if rating:
        candidate["ratings"] = {"simkl": {"rating": rating}}
    return {"candidate": candidate, "section": "tv", "genre": genres[0].lower()}


# ------------------------------------------------------------------- profile


def test_profile_counts_accepted_genres_and_decades():
    items = [
        WatchItem(title="A", year=2014, genres=["Drama", "Crime"]),
        WatchItem(title="B", year=2016, genres=["Crime"]),
        WatchItem(title="C", year=1998, genres=["Comedy"]),
    ]
    profile = build_profile(items)

    assert profile.accepted == 3
    assert profile.accepted_genres["crime"] == 2
    assert profile.decades[2010] == 2
    assert profile.decades[1990] == 1
    assert not profile.empty


def test_rejections_push_a_genre_negative():
    profile = build_profile(
        [WatchItem(title="A", genres=["crime"])],
        rejected_genres=[["romance"], ["romance"], ["romance"]],
    )
    affinity = profile.genre_affinity()

    assert affinity["crime"] > 0
    assert affinity["romance"] < 0
    assert "romance" not in profile.top_genres()


def test_empty_profile_reports_itself():
    profile = TasteProfile()
    assert profile.empty
    assert profile.top_genres() == []
    assert "No taste profile yet" in profile.summary()


@pytest.mark.parametrize("year,expected", [(2014, 2010), (1999, 1990), (None, None)])
def test_decade(year, expected):
    assert decade(year) == expected


# ------------------------------------------------------------------- ranking


def test_idf_damps_ubiquitous_genres():
    entries = [entry(f"Show {n}", ["Drama"]) for n in range(9)]
    entries.append(entry("Odd One", ["Drama", "Cyberpunk"]))
    idf = genre_idf(entries)

    assert idf["cyberpunk"] > idf["drama"]


def test_ranking_puts_the_matching_genre_first():
    profile = build_profile([WatchItem(title="A", genres=["crime"], year=2015)])
    entries = [
        entry("Romantic Thing", ["Romance"]),
        entry("Cooking Thing", ["Documentary"]),
        entry("Crime Thing", ["Crime"]),
    ]
    ranked = rank_entries(entries, profile)

    assert ranked[0][0]["candidate"]["title"] == "Crime Thing"
    assert ranked[0][1] == pytest.approx(100.0)
    assert ranked[-1][1] == pytest.approx(0.0)


def test_ranking_keeps_api_order_when_nothing_is_known():
    entries = [entry("First", ["Drama"]), entry("Second", ["Crime"])]
    ranked = rank_entries(entries, TasteProfile())

    assert [e["candidate"]["title"] for e, _ in ranked] == ["First", "Second"]
    assert all(score == 0.0 for _, score in ranked)


def test_rating_breaks_ties_between_equal_genres():
    profile = build_profile([WatchItem(title="A", genres=["crime"])])
    entries = [entry("Meh", ["Crime"], rating=5.0), entry("Great", ["Crime"], rating=9.0)]
    ranked = rank_entries(entries, profile)

    assert ranked[0][0]["candidate"]["title"] == "Great"


def test_score_handles_candidates_with_no_genres():
    profile = build_profile([WatchItem(title="A", genres=["crime"])])
    assert score_candidate({"title": "Mystery Meat"}, profile, {}) == pytest.approx(0.0)


def test_candidate_genres_is_forgiving():
    assert candidate_genres({"genres": ["Drama", " Crime "]}) == ["drama", "crime"]
    assert candidate_genres({"genres": None}) == []
    assert candidate_genres({}) == []


# ------------------------------------------------------------------- posters


def test_poster_url_shape():
    url = poster_url("54/5465988836c0aaa33", width=44)
    assert url.startswith("https://wsrv.nl/?url=https://simkl.in/posters/54/5465988836c0aaa33_c.jpg")
    assert "w=44" in url
    assert poster_url("") == ""


class FakeSession:
    """Serves one generated JPEG, and counts how often it was asked for it."""

    def __init__(self):
        self.calls = 0
        buffer = io.BytesIO()
        Image.new("RGB", (40, 60), (10, 120, 200)).save(buffer, format="JPEG")
        self.payload = buffer.getvalue()

    def get(self, url, timeout=None):
        self.calls += 1
        return type("R", (), {"status_code": 200, "content": self.payload})()


def test_render_lines_paints_half_blocks(tmp_path):
    session = FakeSession()
    renderer = PosterRenderer(session, tmp_path, width=10)
    renderer.enabled = True  # bypass the tty/colour check for the test

    lines = renderer.render_lines("54/abc")
    assert lines, "expected rendered rows"
    # 10 wide, poster is 2:3, so 15 pixel rows -> rounded to 16 -> 8 cells
    assert len(lines) == 8
    assert lines[0].count("▀") == 10
    assert "\033[38;2;" in lines[0] and "\033[48;2;" in lines[0]


def test_posters_are_only_downloaded_once(tmp_path):
    session = FakeSession()
    renderer = PosterRenderer(session, tmp_path, width=8)
    renderer.enabled = True

    renderer.render_lines("54/abc")
    renderer.render_lines("54/abc")
    assert session.calls == 1  # second call served from the on-disk cache


def test_render_beside_pads_to_the_taller_column(tmp_path):
    session = FakeSession()
    renderer = PosterRenderer(session, tmp_path, width=8)
    renderer.enabled = True

    output = renderer.render_beside("54/abc", ["one", "two"])
    art_rows = len(renderer.render_lines("54/abc"))
    assert len(output.splitlines()) == max(art_rows, 2)
    assert "one" in output and "two" in output


def test_renderer_degrades_without_a_poster(tmp_path):
    renderer = PosterRenderer(FakeSession(), tmp_path, width=8)
    renderer.enabled = True
    assert renderer.render_lines("") == []
    assert renderer.render_beside("", ["just text"]) == "just text"


def test_disabled_renderer_explains_itself(tmp_path):
    renderer = PosterRenderer(FakeSession(), tmp_path, enabled=False)
    assert not renderer.enabled
    assert renderer.render_lines("54/abc") == []


def test_watchitem_roundtrips_posters_and_genres():
    item = WatchItem(
        title="Severance",
        media_type=TV,
        year=2022,
        ids={"simkl": 1},
        poster="54/abc",
        genres=["drama", "mystery"],
    )
    restored = WatchItem.from_dict(item.to_dict())
    assert restored.poster == "54/abc"
    assert restored.genres == ["drama", "mystery"]


def test_poster_and_genres_survive_a_merge():
    first = WatchItem(title="Severance", media_type=MOVIE)
    second = WatchItem(title="Severance", media_type=MOVIE, poster="54/abc", genres=["drama"])
    first.merge(second)
    assert first.poster == "54/abc"
    assert first.genres == ["drama"]


def test_default_poster_width_is_sane():
    assert 8 <= DEFAULT_WIDTH <= 60
