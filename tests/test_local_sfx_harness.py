"""Local smoke-test helpers: compact prompt, catalog ids, and a silent bed."""

import json
from pathlib import Path

from app.schemas.deepgram import DeepgramTranscript
from scripts.local_sfx_test.run_local_sfx_test import (
    DEFAULT_CATALOG,
    DEFAULT_STORY,
    compact_story,
    load_catalog_ids,
    resolve_clips,
    stitch_bed,
    validate_cues,
)
from tests.wavutil import sine_wav_bytes


def test_story_fixture_is_a_long_deepgram_listen_payload():
    payload = json.loads(DEFAULT_STORY.read_text(encoding="utf-8"))
    transcript = DeepgramTranscript.model_validate(payload).normalized()
    assert transcript.duration_seconds >= 40
    assert len(payload["results"]["utterances"]) >= 8
    alternative = payload["results"]["channels"][0]["alternatives"][0]
    assert alternative["words"]
    assert alternative["paragraphs"]["paragraphs"]
    assert payload["metadata"]["models"]
    text = transcript.text.lower()
    for theme in (
        "rain",
        "creaky door",
        "bird",
        "bark",
        "footsteps",
        "wind",
        "thunder",
        "page",
        "chime",
        "kettle",
        "owl",
        "yawn",
    ):
        assert theme in text


def test_compact_prompt_lists_catalog_ids_only():
    payload = json.loads(DEFAULT_STORY.read_text(encoding="utf-8"))
    compact = compact_story(payload, ["rain", "door-creak"])
    encoded = json.dumps(compact)
    assert compact["available_catalog"] == ["rain", "door-creak"]
    assert compact["duration_seconds"] >= 40
    assert compact["words"][0].keys() == {"word", "start", "end"}
    assert "text" in compact["utterances"][0]
    for leaked in ("confidence", "model_info", "previews", "license", "freesound"):
        assert leaked not in encoded


def test_unknown_catalog_ids_are_dropped():
    accepted, dropped = validate_cues(
        [
            {"catalog_id": "rain", "start": 1, "end": 3, "reason": "rain"},
            {"catalog_id": "spaceship", "start": 4, "end": 5, "reason": "no"},
        ],
        {"rain"},
        50,
    )
    assert [cue["catalog_id"] for cue in accepted] == ["rain"]
    assert dropped[0]["catalog_id"] == "spaceship"


def test_clip_lookup_uses_the_catalog_id_as_the_filename(tmp_path: Path):
    clips = tmp_path / "catalog_clips"
    clips.mkdir()
    (clips / "rain.mp3").write_bytes(b"not-really-mp3")
    (clips / "door-creak.wav").write_bytes(b"RIFFstub")
    found = resolve_clips(["rain", "door-creak", "owl-hoot"], [clips])
    assert found["rain"].name == "rain.mp3"
    assert found["door-creak"].name == "door-creak.wav"
    assert "owl-hoot" not in found


def test_stitch_places_a_local_clip_on_the_story_bed(tmp_path: Path):
    wav = tmp_path / "rain.wav"
    wav.write_bytes(sine_wav_bytes(800, frequency=440))
    output = tmp_path / "out" / "sfx_bed.mp3"
    used, missing = stitch_bed(
        [{"catalog_id": "rain", "start": 0.2, "end": 0.7, "reason": "rain"}],
        {"rain": wav},
        2.0,
        output,
    )
    assert used == ["rain"]
    assert missing == []
    assert output.is_file() and output.stat().st_size > 100


def test_catalog_ids_come_from_the_checked_in_catalog():
    ids = load_catalog_ids(DEFAULT_CATALOG)
    assert "rain" in ids
    assert "door-creak" in ids
    assert "yawn" in ids
    assert len(ids) >= 30
