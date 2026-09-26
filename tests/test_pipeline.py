"""Pipeline keeps every overlapping cue for the mixer."""

import json
from pathlib import Path

from app.schemas.deepgram import DeepgramTranscript
from app.schemas.sfx import SfxCue
from app.services.freesound import DownloadedClip
from app.services.pipeline import run_pipeline
from tests.wavutil import sine_wav_bytes

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "deepgram_sample.json"


class _Plan:
    def __init__(self, cues: list[SfxCue]) -> None:
        self._cues = cues

    def plan_cues(self, transcript, catalog=None):
        return self._cues


class _Clips:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def download_for_query(self, query: str) -> DownloadedClip:
        self.queries.append(query)
        return DownloadedClip(
            sound_id=1,
            name=query,
            query=query,
            preview_url="https://cdn.example/clip.wav",
            audio_bytes=sine_wav_bytes(500, frequency=440),
            duration_seconds=0.5,
        )


def test_overlapping_ambient_and_oneshot_both_reach_the_mixer(monkeypatch):
    captured: dict = {}

    def fake_mix(clips, duration_ms):
        captured["clips"] = clips
        captured["duration_ms"] = duration_ms
        return b"ID3"

    monkeypatch.setattr("app.services.pipeline.mix_sfx_bytes", fake_mix)
    transcript = DeepgramTranscript.model_validate(json.loads(FIXTURE.read_text())).normalized()
    freesound = _Clips()
    output = run_pipeline(
        transcript,
        _Plan(
            [
                SfxCue(
                    catalog_id="rain",
                    reason="Rain over the opening.",
                    kind="ambient",
                    start=0.35,
                    end=14.35,
                    gain_db=-8,
                ),
                SfxCue(
                    catalog_id="door-creak",
                    reason="The creaky door on top of the rain.",
                    kind="oneshot",
                    start=5.5,
                    end=6.4,
                ),
            ]
        ),
        freesound,
    )

    assert output.warnings == []
    assert freesound.queries == ["rain", "door-creak"]
    clips = captured["clips"]
    assert [(clip.query, clip.kind, clip.start_ms, clip.end_ms, clip.gain_db) for clip in clips] == [
        ("rain", "ambient", 350, 14350, -8.0),
        ("door-creak", "oneshot", 5500, 6400, None),
    ]
    assert clips[0].start_ms < clips[1].start_ms < clips[0].end_ms
    assert captured["duration_ms"] == 55780
