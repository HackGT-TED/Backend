"""Story routes: process a transcript, list recordings, redirect to the SFX URL."""

import json
import uuid
from datetime import datetime, timezone
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError

from app.schemas.api import (
    CueOut,
    ProcessStoryRequest,
    RecordingDetail,
    RecordingSummary,
    StoryCover,
    StoryDescription,
    TranscriptOut,
    TranscriptSegmentOut,
    TranscriptWordOut,
)
from app.schemas.deepgram import DeepgramTranscript
from app.schemas.sfx import SfxCue
from app.services.imagine import cover_prompt
from app.services.deepgram import DeepgramAuthError, DeepgramError, DeepgramNotConfiguredError
from app.services.freesound import FreeSoundNotConfiguredError, FreeSoundRateLimitError
from app.services.mixer import AudioJoinError, AudioMixError, join_audio
from app.services.pipeline import PipelineOutput, run_pipeline
from app.services.transcribe import (
    TranscriptionError,
    TranscriptionNotConfiguredError,
    transcribe_audio,
)
from app.services.store import (
    RecordingRecord,
    RecordingStore,
    SupabaseError,
    SupabaseNotConfiguredError,
)
from app.services.xai import XaiAuthError, XaiError, XaiNotConfiguredError

router = APIRouter(prefix="/stories", tags=["stories"])

_MAX_AUDIO_BYTES = 25 * 1024 * 1024


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

    catalog = _catalog(request)
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
            catalog=catalog,
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


@router.post("/transcribe", response_model=TranscriptOut)
async def transcribe_story(request: Request) -> TranscriptOut:
    """Transcribe an audio URL or uploaded file with Deepgram. Does not mix SFX."""

    raw, _meta = await _transcribe_request(request)
    return _transcript_out(raw)


@router.post("/render")
async def render_story(request: Request) -> Response:
    """Upload a recording and get it back with catalog effects lined up on that audio.

    Does not write a Supabase row. The response body is the mixed MP3. Its
    length is the decoded upload, and each effect starts at the cue time from
    the transcript of those same bytes.
    """

    if "multipart/form-data" not in request.headers.get("content-type", ""):
        raise HTTPException(
            status_code=422,
            detail="Send multipart form data with an audio file field named audio",
        )
    data, mime, filename = await _read_story_audio(request)
    output = await run_in_threadpool(_render_upload, request, data, mime, filename)
    return _audio_response(output)


@router.post("/describe", response_model=StoryDescription)
async def describe_story(request: Request) -> StoryDescription:
    """Transcribe a recording and return a short catalog card. Does not mix audio.

    Multipart ``audio`` uses the same transcriber as ``/stories/render``.
    A JSON body ``{"url": "..."}`` asks Deepgram to fetch that URL.
    """

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        data, mime, filename = await _read_story_audio(request)
        return await run_in_threadpool(_describe_upload, request, data, mime, filename)
    if "application/json" in content_type:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON body must be an object")
        url = payload.get("url") or payload.get("audio_url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=422, detail="JSON body must include url")
        return await run_in_threadpool(_describe_url, request, url.strip())
    raise HTTPException(
        status_code=422,
        detail="Send a JSON body with url, or multipart form data with an audio file",
    )


@router.post("/cover", response_model=StoryCover)
async def cover_story(request: Request) -> StoryCover:
    """Transcribe a recording, write a catalog card, and draw one picture-book cover.

    Same audio input as ``/stories/describe``. Does not plan cues or mix audio.
    The image URL is temporary.
    """

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        data, mime, filename = await _read_story_audio(request)
        return await run_in_threadpool(_cover_upload, request, data, mime, filename)
    if "application/json" in content_type:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON body must be an object")
        url = payload.get("url") or payload.get("audio_url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=422, detail="JSON body must include url")
        return await run_in_threadpool(_cover_url, request, url.strip())
    raise HTTPException(
        status_code=422,
        detail="Send a JSON body with url, or multipart form data with an audio file",
    )


@router.post("/process-audio", response_model=RecordingDetail, status_code=201)
async def process_audio(request: Request) -> RecordingDetail:
    """Transcribe audio with Deepgram, then run the SFX pipeline and store it."""

    raw, meta = await _transcribe_request(request)
    body = ProcessStoryRequest(
        story_id=_optional_str(meta.get("story_id")),
        title=_optional_str(meta.get("title")),
        narrator=_optional_str(meta.get("narrator")),
        source_audio_url=_optional_str(meta.get("source_audio_url")) or _optional_str(meta.get("url")),
        deepgram=raw,
    )
    return await run_in_threadpool(process_story, request, body)


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


async def _transcribe_request(request: Request) -> tuple[dict, dict]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON body must be an object")
        url = payload.get("url") or payload.get("audio_url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=422, detail="JSON body must include url")
        raw = _call_deepgram(lambda: request.app.state.deepgram.transcribe_url(url.strip()))
        return raw, payload
    if "multipart/form-data" in content_type:
        data, mime, _filename, meta = await _read_audio_upload(request)
        raw = _call_deepgram(lambda: request.app.state.deepgram.transcribe_bytes(data, mime))
        return raw, meta
    raise HTTPException(
        status_code=422,
        detail="Send a JSON body with url, or multipart form data with an audio file",
    )


async def _read_story_audio(request: Request) -> tuple[bytes, str, str]:
    """Read one or more ``audio`` files, in form order, as a single recording.

    One file passes through unchanged. Several (a story's moments) are joined
    into one MP3 with a short pause between them. The size limit covers the total.
    """

    form = await request.form()
    uploads = [upload for upload in form.getlist("audio") if hasattr(upload, "read")]
    if not uploads:
        raise HTTPException(status_code=422, detail="Multipart body must include an audio file field")
    parts: list[bytes] = []
    total = 0
    for index, upload in enumerate(uploads, start=1):
        data = await upload.read()
        if not data:
            detail = "Audio file was empty" if len(uploads) == 1 else f"Audio file {index} was empty"
            raise HTTPException(status_code=422, detail=detail)
        total += len(data)
        if total > _MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="Audio file is larger than 25 MB")
        parts.append(data)
    if len(parts) == 1:
        upload = uploads[0]
        mime = getattr(upload, "content_type", None) or "application/octet-stream"
        filename = getattr(upload, "filename", None) or "story.audio"
        return parts[0], mime, filename
    try:
        joined = await run_in_threadpool(join_audio, parts)
    except AudioJoinError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AudioMixError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return joined, "audio/mpeg", "story.mp3"


async def _read_audio_upload(request: Request) -> tuple[bytes, str, str, dict]:
    form = await request.form()
    upload = form.get("audio")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=422, detail="Multipart body must include an audio file field")
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=422, detail="Audio file was empty")
    if len(data) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio file is larger than 25 MB")
    mime = getattr(upload, "content_type", None) or "application/octet-stream"
    filename = getattr(upload, "filename", None) or "story.audio"
    meta = {key: form.get(key) for key in ("story_id", "title", "narrator", "source_audio_url", "url")}
    return data, mime, filename, meta


def _render_upload(request: Request, data: bytes, mime: str, filename: str) -> PipelineOutput:
    try:
        raw = transcribe_audio(
            request.app.state.settings,
            request.app.state.deepgram,
            data,
            mime,
            filename,
        )
    except (TranscriptionNotConfiguredError, DeepgramNotConfiguredError, DeepgramAuthError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (TranscriptionError, DeepgramError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        transcript = DeepgramTranscript.model_validate(raw).normalized()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        return run_pipeline(
            transcript,
            request.app.state.xai,
            request.app.state.freesound,
            story_bytes=data,
            catalog=_catalog(request),
        )
    except (XaiNotConfiguredError, XaiAuthError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except XaiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except FreeSoundNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FreeSoundRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AudioMixError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _describe_upload(request: Request, data: bytes, mime: str, filename: str) -> StoryDescription:
    try:
        raw = transcribe_audio(
            request.app.state.settings,
            request.app.state.deepgram,
            data,
            mime,
            filename,
        )
    except (TranscriptionNotConfiguredError, DeepgramNotConfiguredError, DeepgramAuthError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (TranscriptionError, DeepgramError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _describe_transcript(request, raw, filename)


def _describe_url(request: Request, url: str) -> StoryDescription:
    raw = _call_deepgram(lambda: request.app.state.deepgram.transcribe_url(url))
    return _describe_transcript(request, raw, url)


def _describe_transcript(request: Request, raw: dict, audio: str) -> StoryDescription:
    try:
        transcript = DeepgramTranscript.model_validate(raw).normalized()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    describe = getattr(request.app.state.xai, "describe_story", None)
    if describe is None:
        raise HTTPException(status_code=500, detail="xAI client cannot describe a story")
    try:
        blurb = describe(transcript)
    except (XaiNotConfiguredError, XaiAuthError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except XaiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return StoryDescription(
        audio=audio,
        duration_seconds=transcript.duration_seconds,
        transcript_text=transcript.text,
        description=blurb.description,
        hashtags=list(blurb.hashtags),
    )


def _cover_upload(request: Request, data: bytes, mime: str, filename: str) -> StoryCover:
    try:
        raw = transcribe_audio(
            request.app.state.settings,
            request.app.state.deepgram,
            data,
            mime,
            filename,
        )
    except (TranscriptionNotConfiguredError, DeepgramNotConfiguredError, DeepgramAuthError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (TranscriptionError, DeepgramError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _cover_transcript(request, raw, filename)


def _cover_url(request: Request, url: str) -> StoryCover:
    raw = _call_deepgram(lambda: request.app.state.deepgram.transcribe_url(url))
    return _cover_transcript(request, raw, url)


def _cover_transcript(request: Request, raw: dict, audio: str) -> StoryCover:
    try:
        transcript = DeepgramTranscript.model_validate(raw).normalized()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    picture = getattr(request.app.state.xai, "picture_story", None)
    if picture is None:
        raise HTTPException(status_code=500, detail="xAI client cannot describe a story")
    try:
        card = picture(transcript)
    except (XaiNotConfiguredError, XaiAuthError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except XaiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    prompt = cover_prompt(card, transcript.duration_seconds)
    try:
        image_url = request.app.state.imagine.generate(prompt)
    except (XaiNotConfiguredError, XaiAuthError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except XaiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return StoryCover(
        audio=audio,
        duration_seconds=transcript.duration_seconds,
        transcript_text=transcript.text,
        description=card.description,
        hashtags=list(card.hashtags),
        scene=card.scene,
        image_url=image_url,
    )


def _audio_response(output: PipelineOutput) -> Response:
    headers = {
        "Content-Disposition": 'attachment; filename="story_with_sfx.mp3"',
        "X-Story-Duration-Seconds": f"{output.duration_seconds:.3f}",
        "X-Story-Cue-Count": str(len(output.cues)),
    }
    if output.warnings:
        warning = " | ".join(output.warnings).replace("\n", " ")
        headers["X-Story-Warnings"] = warning.encode("latin-1", errors="replace").decode("latin-1")[:500]
    return Response(content=output.audio_bytes, media_type="audio/mpeg", headers=headers)


def _call_deepgram(call) -> dict:
    try:
        return call()
    except (DeepgramNotConfiguredError, DeepgramAuthError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DeepgramError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _transcript_out(raw: dict) -> TranscriptOut:
    try:
        transcript = DeepgramTranscript.model_validate(raw).normalized()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TranscriptOut(
        transcript_text=transcript.text,
        duration_seconds=transcript.duration_seconds,
        words=[
            TranscriptWordOut(word=word.display, start=word.start, end=word.end)
            for word in transcript.words
        ],
        segments=[
            TranscriptSegmentOut(text=segment.text, start=segment.start, end=segment.end)
            for segment in transcript.segments
        ],
        deepgram=raw,
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _store(request: Request) -> RecordingStore:
    return request.app.state.store


def _catalog(request: Request) -> dict:
    """Active SFX catalog for this request (Supabase, then the checked-in file)."""

    try:
        catalog = request.app.state.catalog_loader()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not isinstance(catalog, dict):
        raise HTTPException(status_code=503, detail="SFX catalog could not be read")
    return catalog


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
