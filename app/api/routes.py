"""Story routes: process a transcript, list recordings, serve the SFX MP3."""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.db.models import Recording, StoryAudio
from app.schemas.api import CueOut, ProcessStoryRequest, RecordingDetail, RecordingSummary
from app.schemas.deepgram import DeepgramTranscript
from app.schemas.sfx import SfxCue
from app.services.freesound import FreeSoundNotConfiguredError, FreeSoundRateLimitError
from app.services.mixer import AudioMixError
from app.services.muse_spark import MuseSparkAuthError, MuseSparkError, MuseSparkNotConfiguredError
from app.services.pipeline import run_pipeline

router = APIRouter(prefix="/stories", tags=["stories"])


def get_db(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


@router.post("/process", response_model=RecordingDetail, status_code=201)
def process_story(
    request: Request,
    body: ProcessStoryRequest,
    db: Session = Depends(get_db),
) -> RecordingDetail:
    """Plan SFX cues, mix an MP3 aligned to the transcript, and store it."""

    try:
        payload = body.deepgram_payload()
        transcript = DeepgramTranscript.model_validate(payload).normalized()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    settings: Settings = request.app.state.settings
    recording = Recording(
        id=str(uuid.uuid4()),
        story_id=body.story_id,
        title=body.title,
        narrator=body.narrator,
        source_audio_url=body.source_audio_url,
        transcript_text=transcript.text,
        transcript_json=payload,
        duration_seconds=transcript.duration_seconds,
        status="processing",
        warnings_json=[],
    )
    db.add(recording)
    db.commit()

    try:
        output = run_pipeline(
            transcript,
            request.app.state.muse,
            request.app.state.freesound,
            Path(settings.media_dir),
            recording.id,
        )
    except (MuseSparkNotConfiguredError, MuseSparkAuthError) as exc:
        _mark_failed(db, recording, exc)
        _raise_pipeline_error(503, exc, recording.id)
    except MuseSparkError as exc:
        _mark_failed(db, recording, exc)
        _raise_pipeline_error(502, exc, recording.id)
    except FreeSoundNotConfiguredError as exc:
        _mark_failed(db, recording, exc)
        _raise_pipeline_error(503, exc, recording.id)
    except FreeSoundRateLimitError as exc:
        _mark_failed(db, recording, exc)
        _raise_pipeline_error(429, exc, recording.id)
    except AudioMixError as exc:
        _mark_failed(db, recording, exc)
        _raise_pipeline_error(500, exc, recording.id)
    except Exception as exc:
        _mark_failed(db, recording, exc)
        raise

    audio = StoryAudio(
        recording_id=recording.id,
        sfx_mp3_path=output.relative_path,
        cues_json=[_cue_payload(cue) for cue in output.cues],
        duration_seconds=output.duration_seconds,
    )
    recording.story_audio = audio
    recording.status = "ready"
    recording.duration_seconds = output.duration_seconds
    recording.warnings_json = output.warnings
    recording.error_message = None
    db.add(audio)
    db.commit()
    return _detail(recording, request)


@router.get("", response_model=list[RecordingSummary])
def list_stories(
    request: Request,
    story_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[RecordingSummary]:
    stmt = (
        select(Recording)
        .options(selectinload(Recording.story_audio))
        .order_by(Recording.created_at.desc())
    )
    if story_id is not None:
        stmt = stmt.where(Recording.story_id == story_id)
    rows = db.scalars(stmt).all()
    return [_summary(row, request) for row in rows]


@router.get("/{recording_id}", response_model=RecordingDetail)
def get_story(
    recording_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RecordingDetail:
    return _detail(_get_recording(db, recording_id), request)


@router.get("/{recording_id}/sfx", name="download_sfx")
def download_sfx(
    recording_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> FileResponse:
    recording = _get_recording(db, recording_id)
    if recording.status != "ready" or recording.story_audio is None:
        raise HTTPException(status_code=404, detail="SFX track is not available")
    path = _media_file(request.app.state.settings, recording.story_audio.sfx_mp3_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="SFX file is missing")
    return FileResponse(path, media_type="audio/mpeg", filename=f"{recording.id}.mp3")


def _get_recording(db: Session, recording_id: str) -> Recording:
    stmt = (
        select(Recording)
        .options(selectinload(Recording.story_audio))
        .where(Recording.id == recording_id)
    )
    recording = db.scalars(stmt).first()
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return recording


def _mark_failed(db: Session, recording: Recording, exc: Exception) -> None:
    recording.status = "failed"
    recording.error_message = str(exc)
    db.commit()


def _raise_pipeline_error(status_code: int, exc: Exception, recording_id: str) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail={"message": str(exc), "recording_id": recording_id},
    )


def _cue_payload(cue: SfxCue) -> dict:
    start_ms, end_ms = cue.window_ms()
    return {
        "query": cue.query,
        "description": cue.description,
        "start": start_ms / 1000,
        "end": end_ms / 1000,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }


def _sfx_url(request: Request, recording: Recording) -> str | None:
    if recording.status != "ready" or recording.story_audio is None:
        return None
    return str(request.url_for("download_sfx", recording_id=recording.id))


def _summary(recording: Recording, request: Request) -> RecordingSummary:
    return RecordingSummary(
        id=recording.id,
        story_id=recording.story_id,
        title=recording.title,
        narrator=recording.narrator,
        status=recording.status,
        duration_seconds=recording.duration_seconds,
        sfx_url=_sfx_url(request, recording),
        created_at=recording.created_at,
    )


def _detail(recording: Recording, request: Request) -> RecordingDetail:
    cues_raw = recording.story_audio.cues_json if recording.story_audio else []
    return RecordingDetail(
        **_summary(recording, request).model_dump(),
        source_audio_url=recording.source_audio_url,
        transcript_text=recording.transcript_text,
        cues=[CueOut.model_validate(item) for item in cues_raw or []],
        warnings=list(recording.warnings_json or []),
        error_message=recording.error_message,
        transcript_json=recording.transcript_json or {},
    )


def _media_file(settings: Settings, relative: str) -> Path:
    root = Path(settings.media_dir).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Invalid media path")
    return path
