"""Catalog matching and the one-shot FreeSound ranking."""

from app.services.sfx_catalog import (
    CATALOG_PATH,
    load_catalog,
    match_entry,
    score_candidate,
    search_params,
    select_best_sound,
    slot_needs_search,
)


def _entry(**overrides) -> dict:
    data = {
        "id": "door-creak",
        "label": "Door creak",
        "category": "home",
        "keywords": ["door", "creak", "creaked", "wooden door"],
        "status": "empty",
        "preview_url": "https://cdn.example/door.mp3",
        "search_query": "wooden door creak",
        "preferred_tags": ["door", "creak"],
        "min_duration": 0.2,
        "max_duration": 4,
    }
    data.update(overrides)
    return data


def test_checked_in_catalog_lists_picture_book_slots():
    catalog = load_catalog(CATALOG_PATH)
    entries = catalog["entries"]
    assert 30 <= len(entries) <= 120
    ids = [entry["id"] for entry in entries]
    assert len(ids) == len(set(ids))
    categories = {entry["category"] for entry in entries}
    assert {"animals", "nature", "home", "magic", "transport", "story", "time"} <= categories
    filled = 0
    for entry in entries:
        assert entry["status"] in {"empty", "pending", "approved", "rejected"}
        assert entry["keywords"]
        assert entry["search_query"]
        assert entry["description"]
        if entry.get("preview_url"):
            filled += 1
            assert str(entry["preview_url"]).startswith("http")
            assert entry["freesound_id"]
    assert filled >= 30


def test_cue_matches_door_even_when_rain_is_listed_first():
    entries = [
        _entry(
            id="rain",
            label="Rain",
            category="nature",
            keywords=["rain", "drizzle", "rainfall"],
            preview_url="https://cdn.example/rain.mp3",
        ),
        _entry(),
    ]
    assert match_entry(entries, "wooden door creak")["id"] == "door-creak"
    assert match_entry(entries, "gentle rain ambience")["id"] == "rain"


def test_real_catalog_maps_common_story_cues():
    entries = load_catalog(CATALOG_PATH)["entries"]
    expect = {
        "wooden door creak": "door-creak",
        "gentle rain ambience": "rain",
        "rain on the window": "rain-window",
        "owl at night": "owl-hoot",
        "night crickets": "crickets",
        "bicycle bell": "bicycle-bell",
        "soft bell": "soft-bell",
        "children laughter": "kids-laughter",
        "footsteps on a wooden floor": "footsteps-wood",
        "dog bark": "dog-bark",
    }
    for query, slot_id in expect.items():
        assert match_entry(entries, query)["id"] == slot_id
    assert match_entry(entries, "spaceship laser") is None


def test_rejected_slot_is_not_a_match():
    entries = [_entry(status="rejected"), _entry(id="rain", label="Rain", keywords=["rain"])]
    assert match_entry(entries, "wooden door creak") is None


def test_equal_scores_break_by_stable_id():
    entries = [
        _entry(id="zeta", label="Bell", keywords=["bell"]),
        _entry(id="alpha", label="Bell", keywords=["bell"]),
    ]
    assert match_entry(entries, "bell")["id"] == "alpha"


def test_scorer_prefers_a_later_cc0_hit_over_the_first_result():
    slot = _entry()
    first = _sound(
        1,
        name="door",
        tags=["door"],
        license="Attribution",
        avg_rating=2.0,
        num_downloads=4,
    )
    better = _sound(
        2,
        name="wooden door creak",
        tags=["door", "creak", "wood"],
        license="Creative Commons 0",
        avg_rating=4.8,
        num_downloads=5000,
    )
    assert select_best_sound([first, better], slot, set())["id"] == 2
    assert select_best_sound([better, first], slot, set())["id"] == 2
    assert select_best_sound([better, first], slot, {2})["id"] == 1


def test_scorer_drops_music_and_long_loops():
    slot = _entry(max_duration=8)
    music = _sound(3, name="door song", tags=["music", "song"], num_downloads=90000, avg_rating=5)
    too_long = _sound(4, name="door", tags=["door", "loop"], duration=7.5, num_downloads=90000, avg_rating=5)
    usable = _sound(5, name="door creak", tags=["door", "creak"])
    assert score_candidate(music, slot, set()) is None
    assert score_candidate(too_long, slot, set()) is None
    assert select_best_sound([music, too_long, usable], slot, set())["id"] == 5


def test_search_params_filter_duration_and_license():
    params = search_params(_entry(min_duration=0.3, max_duration=4, search_query="wooden door creak"))
    assert params["query"] == "wooden door creak"
    assert params["sort"] == "rating_desc"
    assert "duration:[0.3 TO 4]" in params["filter"]
    assert 'license:("Creative Commons 0" OR "Attribution")' in params["filter"]
    assert params["page_size"] > 1


def test_approved_and_pending_slots_are_not_searched_again():
    assert slot_needs_search({"status": "approved", "freesound_id": 9}, refresh=True) is False
    assert slot_needs_search({"status": "pending", "freesound_id": 9}, refresh=False) is False
    assert slot_needs_search({"status": "pending", "freesound_id": 9}, refresh=True) is True
    assert slot_needs_search({"status": "rejected", "freesound_id": 9}, refresh=False) is True
    assert slot_needs_search({"status": "empty"}, refresh=False) is True


def _sound(sound_id: int, **overrides) -> dict:
    data = {
        "id": sound_id,
        "name": "clip",
        "tags": ["door"],
        "duration": 1.2,
        "license": "Attribution",
        "avg_rating": 3.0,
        "num_downloads": 20,
        "previews": {"preview-hq-mp3": f"https://cdn.example/{sound_id}.mp3"},
    }
    data.update(overrides)
    return data


def test_trim_start_is_read_from_the_catalog_entry_in_milliseconds():
    from app.services.pipeline import trim_start_ms

    assert trim_start_ms({"trim_start_s": 2.5}) == 2_500
    assert trim_start_ms({"trim_start_s": 0.8}) == 800
    for bad in ({}, None, {"trim_start_s": -1}, {"trim_start_s": "2"}, {"trim_start_s": True}):
        assert trim_start_ms(bad) == 0


def test_checked_in_trims_are_shorter_than_their_clips():
    entries = load_catalog(CATALOG_PATH)["entries"]
    trimmed = [entry for entry in entries if "trim_start_s" in entry]
    assert {entry["id"] for entry in trimmed} >= {"bear-growl", "door-creak"}
    for entry in trimmed:
        assert 0 < entry["trim_start_s"] < entry["duration"]
