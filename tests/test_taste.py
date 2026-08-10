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


# ----------------------------------------------------------- genre endpoints


class RecordingClient:
    def __init__(self):
        self.paths = []
        self.params = []

    def get(self, path, params=None):
        self.paths.append(path)
        self.params.append(params or {})
        return []


def test_genre_path_defaults_to_all_time_rank():
    from simkl_importer.client import SimklClient

    client = RecordingClient()
    SimklClient.genre_titles(client, "tv", genre="drama")
    assert client.paths == ["/tv/genres/drama/all-types/all-countries/all-networks/all-years/rank"]


def test_genre_path_takes_an_era_and_a_sort():
    from simkl_importer.client import SimklClient

    client = RecordingClient()
    SimklClient.genre_titles(client, "movies", genre="crime", sort="popular-today", year="2000s")
    assert client.paths == ["/movies/genres/crime/all-types/all-countries/2000s/popular-today"]


def test_anime_genre_path_has_no_country_segment():
    from simkl_importer.client import SimklClient

    client = RecordingClient()
    SimklClient.genre_titles(client, "anime", genre="action", year="2010s")
    assert client.paths == ["/anime/genres/action/all-types/all-networks/2010s/rank"]


# ---------------------------------------------------------------- pagination


class PagingClient:
    """Serves `pages` pages per genre, with unique ids, and records the calls."""

    def __init__(self, pages=3, per_page=10, start=1000):
        self.pages = pages
        self.per_page = per_page
        self.next_id = start
        self.calls = []

    def genre_titles(self, section, genre="all", sort="rank", limit=20, page=1, year="all-years"):
        self.calls.append((section, genre, page))
        if page > self.pages:
            return []
        out = []
        for _ in range(self.per_page):
            self.next_id += 1
            out.append({"title": f"T{self.next_id}", "year": 2015, "genres": ["Drama"],
                        "ids": {"simkl_id": self.next_id}})
        return out


def test_gathering_pages_until_the_target_is_met():
    from simkl_importer.discovery import gather_candidates

    client = PagingClient(pages=5, per_page=10)
    got = gather_candidates(client, ["tv"], ["drama"], per_genre=10, target=25, log=lambda *_: None)

    assert len(got) >= 25
    # it had to turn pages - one page of ten could never have reached 25
    assert max(page for _, _, page in client.calls) >= 3


def test_gathering_stops_as_soon_as_the_target_is_met():
    from simkl_importer.discovery import gather_candidates

    client = PagingClient(pages=8, per_page=10)
    gather_candidates(client, ["tv"], ["drama"], per_genre=10, target=20, log=lambda *_: None)

    # 2 pages is enough for 20; a third would be wasted quota
    assert max(page for _, _, page in client.calls) <= 3


def test_already_answered_titles_do_not_count_towards_the_target():
    """The whole point: 300 rejections must not stop it finding fresh ones."""
    from simkl_importer.discovery import gather_candidates

    client = PagingClient(pages=6, per_page=10)
    # everything on the first three pages is already answered
    stale = {str(i) for i in range(1001, 1031)}
    got = gather_candidates(
        client, ["tv"], ["drama"], per_genre=10, exclude=stale, target=15, log=lambda *_: None
    )

    fresh = [e for e in got if str(e["candidate"]["ids"]["simkl_id"]) not in stale]
    assert len(fresh) >= 15
    assert max(page for _, _, page in client.calls) >= 5


def test_gathering_gives_up_when_every_genre_runs_dry():
    from simkl_importer.discovery import gather_candidates

    client = PagingClient(pages=2, per_page=5)
    got = gather_candidates(
        client, ["tv"], ["drama"], per_genre=5, target=500, max_pages=8, log=lambda *_: None
    )

    assert len(got) == 10  # 2 pages x 5, then it stopped
    assert max(page for _, _, page in client.calls) == 3  # one empty page proves the end


def test_no_target_means_a_single_page_as_before():
    from simkl_importer.discovery import gather_candidates

    client = PagingClient(pages=5, per_page=10)
    got = gather_candidates(client, ["tv"], ["drama"], per_genre=10, log=lambda *_: None)

    assert len(got) == 10
    assert [page for _, _, page in client.calls] == [1]


def test_pages_sweep_all_genres_before_going_deeper():
    from simkl_importer.discovery import gather_candidates

    client = PagingClient(pages=5, per_page=2)
    gather_candidates(
        client, ["tv"], ["drama", "crime", "comedy"], per_genre=2, target=10, log=lambda *_: None
    )

    first_pass = [genre for _, genre, page in client.calls if page == 1]
    assert first_pass == ["drama", "crime", "comedy"]


# ------------------------------------------------------- shortlisting by match


def test_the_shortlist_is_the_best_matches_not_the_first_arrivals(monkeypatch, tmp_path):
    """400 examined, 100 shown - and the 100 must be the closest ones."""
    from simkl_importer.config import Store
    from simkl_importer.models import WatchItem
    import simkl_importer.discovery as discovery

    # a pool where only the later-paged titles match the profile
    def fake_gather(client, sections, genres, per_genre, **kwargs):
        out = []
        for n in range(40):
            genre = "crime" if n >= 30 else "romance"   # the good ones arrive last
            out.append({
                "candidate": {"title": f"T{n}", "year": 2015, "genres": [genre.title()],
                              "ids": {"simkl_id": n}},
                "section": "tv", "genre": genre,
            })
        return out

    monkeypatch.setattr(discovery, "gather_candidates", fake_gather)
    store = Store(tmp_path / "home")
    prepared = {
        "library": {},
        "profile": discovery.build_profile([WatchItem(title="seed", genres=["crime"])]),
        "suggested": ["crime"],
    }
    data = discovery.build_session(
        None, store, [], prepared, sections=["tv"], per_genre=10, target=10,
        log=lambda *_: None,
    )

    assert len(data["ranked"]) == 10
    shown = [e["candidate"]["title"] for e, _ in data["ranked"]]
    # the crime ones are T30..T39 - all ten must have made the cut
    assert set(shown) == {f"T{n}" for n in range(30, 40)}
    assert all(score >= 50 for _, score in data["ranked"])


def test_no_target_shows_everything_found(monkeypatch, tmp_path):
    from simkl_importer.config import Store
    import simkl_importer.discovery as discovery

    def fake_gather(client, sections, genres, per_genre, **kwargs):
        return [{"candidate": {"title": f"T{n}", "year": 2015, "genres": ["Drama"],
                               "ids": {"simkl_id": n}}, "section": "tv", "genre": "drama"}
                for n in range(1, 26)]

    monkeypatch.setattr(discovery, "gather_candidates", fake_gather)
    data = discovery.build_session(
        None, Store(tmp_path / "home"), [],
        {"library": {}, "profile": discovery.TasteProfile(), "suggested": []},
        sections=["tv"], per_genre=10, log=lambda *_: None,
    )
    assert len(data["ranked"]) == 25  # the grid is not shortlisted


def test_the_pool_is_bigger_than_what_gets_shown(monkeypatch, tmp_path):
    """The gatherer must be asked for several times the target."""
    from simkl_importer.config import Store
    import simkl_importer.discovery as discovery

    asked = {}

    def fake_gather(client, sections, genres, per_genre, **kwargs):
        asked["target"] = kwargs.get("target")
        asked["genres"] = list(genres)
        return []

    monkeypatch.setattr(discovery, "gather_candidates", fake_gather)
    discovery.build_session(
        None, Store(tmp_path / "home"), [],
        {"library": {}, "profile": discovery.TasteProfile(), "suggested": ["crime"]},
        sections=["tv"], per_genre=10, target=100, log=lambda *_: None,
    )

    assert asked["target"] == 100 * discovery.POOL_FACTOR
    # strongest genre stays first, the rest are there to page through
    assert asked["genres"][0] == "crime"
    assert len(asked["genres"]) > 1


def test_the_pool_is_capped(monkeypatch, tmp_path):
    from simkl_importer.config import Store
    import simkl_importer.discovery as discovery

    asked = {}

    def fake_gather(client, sections, genres, per_genre, **kwargs):
        asked["target"] = kwargs.get("target")
        return []

    monkeypatch.setattr(discovery, "gather_candidates", fake_gather)
    discovery.build_session(
        None, Store(tmp_path / "home"), [],
        {"library": {}, "profile": discovery.TasteProfile(), "suggested": []},
        sections=["tv"], per_genre=10, target=1000, log=lambda *_: None,
    )
    assert asked["target"] == discovery.MAX_POOL


# ------------------------------------------------------- the match is absolute


def test_a_title_sharing_nothing_with_you_scores_zero():
    """The bug: min-max stretching made the least-bad candidate read 100%."""
    from simkl_importer.taste import match_fraction

    profile = build_profile([WatchItem(title="A", genres=["crime", "thriller"])])

    assert match_fraction({"genres": ["Romance"]}, profile) == pytest.approx(0.0)
    assert match_fraction({"genres": ["Documentary", "Music"]}, profile) == pytest.approx(0.0)
    # matching the profile exactly is what earns 100%
    assert match_fraction({"genres": ["Crime", "Thriller"]}, profile) == pytest.approx(1.0)
    # half of it earns notably less
    assert match_fraction({"genres": ["Crime"]}, profile) < 0.8


def test_a_pool_of_bad_matches_stays_a_pool_of_bad_matches():
    """Ranking a poor pool must not promote its least-bad member to 100%."""
    profile = build_profile([WatchItem(title="A", genres=["crime"])])
    entries = [
        entry("K-Drama Thing", ["Romance", "History"]),
        entry("Docu Thing", ["Documentary"]),
        entry("Music Thing", ["Music"]),
    ]
    ranked = rank_entries(entries, profile)

    assert all(score == 0 for _, score in ranked), [s for _, s in ranked]


def test_partial_overlap_lands_in_between():
    from simkl_importer.taste import match_fraction

    profile = build_profile([
        WatchItem(title="A", genres=["crime"]),
        WatchItem(title="B", genres=["crime"]),
        WatchItem(title="C", genres=["comedy"]),
    ])
    both = match_fraction({"genres": ["Crime", "Romance"]}, profile)

    assert 0 < both < 1
    assert both < match_fraction({"genres": ["Crime"]}, profile)


def test_match_is_unknown_rather_than_zero_when_there_is_nothing_to_go_on():
    from simkl_importer.taste import match_fraction

    profile = build_profile([WatchItem(title="A", genres=["crime"])])
    assert match_fraction({"genres": []}, profile) is None
    assert match_fraction({}, profile) is None
    assert match_fraction({"genres": ["Crime"]}, TasteProfile()) is None


def test_a_great_rating_cannot_outrank_actually_matching_you():
    """8.5-rated romance must not beat a mediocre crime show for a crime fan."""
    profile = build_profile([WatchItem(title="A", genres=["crime"])])
    entries = [
        entry("Beloved Romance", ["Romance"], rating=9.6),
        entry("Middling Crime", ["Crime"], rating=6.1),
    ]
    ranked = rank_entries(entries, profile)

    assert ranked[0][0]["candidate"]["title"] == "Middling Crime"


def test_unknown_match_survives_the_payload():
    from simkl_importer.web import candidate_payload

    item = WatchItem(title="No Genres", ids={"simkl": 5})
    blank = {"candidate": {"title": "No Genres", "ids": {"simkl_id": 5}}, "genre": "drama"}
    assert candidate_payload(blank, None, item, True)["match"] is None


# ------------------------------------------------ match compares genre *shapes*


def taste(*genre_lists):
    return build_profile([WatchItem(title=str(i), genres=g)
                          for i, g in enumerate(genre_lists)])


def test_one_genre_in_common_is_not_a_perfect_match():
    """The bug: averaging made a single shared genre score 100%."""
    from simkl_importer.taste import match_fraction

    profile = taste(*([["crime", "thriller"]] * 6 + [["drama"]] * 3 + [["comedy"]] * 2))

    only_crime = match_fraction({"genres": ["Crime"]}, profile)
    both = match_fraction({"genres": ["Crime", "Thriller"]}, profile)

    assert only_crime < 0.75
    assert both > only_crime
    assert both > 0.85


def test_covering_more_of_your_profile_scores_higher():
    from simkl_importer.taste import match_fraction

    profile = taste(["crime", "thriller"], ["crime", "drama"], ["thriller", "drama"])
    scores = [
        match_fraction({"genres": ["Crime"]}, profile),
        match_fraction({"genres": ["Crime", "Thriller"]}, profile),
        match_fraction({"genres": ["Crime", "Thriller", "Drama"]}, profile),
    ]
    assert scores == sorted(scores)


def test_an_unrelated_single_genre_still_scores_zero():
    """The Untamed case: 'Adventure' against a crime/thriller profile."""
    from simkl_importer.taste import match_fraction

    profile = taste(*([["crime", "thriller"]] * 8))
    assert match_fraction({"genres": ["Adventure"]}, profile) == 0.0
    assert match_fraction({"genres": ["Romance", "History"]}, profile) == 0.0


def test_diluting_a_match_with_unrelated_genres_lowers_it():
    from simkl_importer.taste import match_fraction

    profile = taste(*([["crime", "thriller"]] * 8))
    focused = match_fraction({"genres": ["Crime", "Thriller"]}, profile)
    diluted = match_fraction({"genres": ["Crime", "Thriller", "Romance", "Musical"]}, profile)

    assert diluted < focused
