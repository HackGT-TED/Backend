"""Story routes: process a transcript, list recordings, redirect to the SFX URL."""

import json
import uuid
from datetime import datetime, timezone
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.schemas.api import CueOut, ProcessStoryRequest, RecordingDetail, RecordingSummary
from app.schemas.deepgram import DeepgramTranscript
from app.schemas.sfx import SfxCue
from app.services.freesound import FreeSoundNotConfiguredError, FreeSoundRateLimitError
from app.services.mixer import AudioMixError
from app.services.pipeline import run_pipeline
from app.services.store import (
    RecordingRecord,
    RecordingStore,
    SupabaseError,
    SupabaseNotConfiguredError,
)
from app.services.xai import XaiAuthError, XaiError, XaiNotConfiguredError

router = APIRouter(prefix="/stories", tags=["stories"])


@router.post("/process", response_model=RecordingDetail, status_code=201)
def process_story(request: Request, body: ProcessStoryRequest) -> RecordingDetail:
    """Plan SFX cues, mix an MP3, upload it to Supabase Storage, and store the row."""

    try:
        payload = body.deepgram_payload()
        transcript = DeepgramTranscript.model_validate(payload).normalized()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    store = _store(request)
    now = datetime.now(timezone.utc)
    record = RecordingRecord(
        id=str(uuid.uuid4()),
        story_id=body.story_id,
        title=body.title,
        narrator=body.narrator,
        source_audio_url=body.source_audio_url,
        transcript_text=transcript.text,
        transcript_json=payload,
        duration_seconds=transcript.duration_seconds,
        status="processing",
        warnings=[],
        created_at=now,
        updated_at=now,
    )
    _create_record(store, record)

    try:
        output = run_pipeline(
            transcript,
            request.app.state.xai,
            request.app.state.freesound,
        )
    except (XaiNotConfiguredError, XaiAuthError) as exc:
        _mark_failed(store, record, exc)
        _raise_pipeline_error(503, exc, record.id)
    except XaiError as exc:
        _mark_failed(store, record, exc)
        _raise_pipeline_error(502, exc, record.id)
    except FreeSoundNotConfiguredError as exc:
        _mark_failed(store, record, exc)
        _raise_pipeline_error(503, exc, record.id)
    except FreeSoundRateLimitError as exc:
        _mark_failed(store, record, exc)
        _raise_pipeline_error(429, exc, record.id)
    except AudioMixError as exc:
        _mark_failed(store, record, exc)
        _raise_pipeline_error(500, exc, record.id)
    except Exception as exc:
        _mark_failed(store, record, exc)
        raise

    try:
        path, url = store.upload_sfx(record.id, output.audio_bytes)
    except SupabaseNotConfiguredError as exc:
        _mark_failed(store, record, exc)
        _raise_pipeline_error(503, exc, record.id)
    except SupabaseError as exc:
        _mark_failed(store, record, exc)
        _raise_pipeline_error(502, exc, record.id)

    record.status = "ready"
    record.duration_seconds = output.duration_seconds
    record.warnings = output.warnings
    record.error_message = None
    record.cues = [_cue_payload(cue) for cue in output.cues]
    record.sfx_storage_path = path
    record.sfx_url = url
    _save_record(store, record)
    return _detail(record)


@router.get("", response_model=list[RecordingSummary])
def list_stories(request: Request, story_id: str | None = None) -> list[RecordingSummary]:
    try:
        rows = _store(request).list(story_id)
    except SupabaseNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SupabaseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [_summary(row) for row in rows]


@router.get("/{recording_id}", response_model=RecordingDetail)
def get_story(recording_id: str, request: Request) -> RecordingDetail:
    return _detail(_get_recording(request, recording_id))


@router.get("/{recording_id}/sfx", name="download_sfx")
def download_sfx(recording_id: str, request: Request) -> RedirectResponse:
    """Redirect to the public Supabase Storage URL for the SFX MP3."""

    record = _get_recording(request, recording_id)
    if record.status != "ready" or not record.sfx_url:
        raise HTTPException(status_code=404, detail="SFX track is not available")
    return RedirectResponse(record.sfx_url, status_code=307)


def _store(request: Request) -> RecordingStore:
    return request.app.state.store


def _get_recording(request: Request, recording_id: str) -> RecordingRecord:
    try:
        record = _store(request).get(recording_id)
    except SupabaseNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SupabaseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return record


def _create_record(store: RecordingStore, record: RecordingRecord) -> None:
    try:
        store.create(record)
    except SupabaseNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SupabaseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _save_record(store: RecordingStore, record: RecordingRecord) -> None:
    try:
        store.save(record)
    except SupabaseError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": str(exc), "recording_id": record.id},
        ) from exc


def _mark_failed(store: RecordingStore, record: RecordingRecord, exc: Exception) -> None:
    record.status = "failed"
    record.error_message = str(exc)
    try:
        store.save(record)
    except SupabaseError:
        return


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


def _summary(record: RecordingRecord) -> RecordingSummary:
    sfx_url = record.sfx_url if record.status == "ready" else None
    return RecordingSummary(
        id=record.id,
        story_id=record.story_id,
        title=record.title,
        narrator=record.narrator,
        status=record.status,
        duration_seconds=record.duration_seconds,
        sfx_url=sfx_url,
        created_at=record.created_at,
    )


def _detail(record: RecordingRecord) -> RecordingDetail:
    return RecordingDetail(
        **_summary(record).model_dump(),
        source_audio_url=record.source_audio_url,
        transcript_text=record.transcript_text,
        cues=[CueOut.model_validate(item) for item in record.cues or []],
        warnings=list(record.warnings or []),
        error_message=record.error_message,
        transcript_json=record.transcript_json or {},
    )
