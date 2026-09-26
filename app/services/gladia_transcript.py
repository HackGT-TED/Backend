"""Turn Gladia pre-recorded utterances into the Deepgram JSON this pipeline already mixes.

``DeepGram.py`` returns Gladia ``transcription.utterances``. The story SFX path
expects a Deepgram listen document (word ``start``/``end`` plus a duration).
"""

from __future__ import annotations

import json

from app.schemas.deepgram import DeepgramTranscript, NormalizedTranscript


def normalize_gladia(payload: str | list | dict) -> NormalizedTranscript:
    """Accept a JSON string, an utterance list, or a full Gladia result body."""

    return DeepgramTranscript.model_validate(gladia_to_listen_document(payload)).normalized()


def gladia_to_listen_document(payload: str | list | dict) -> dict:
    """Gladia utterances as the Deepgram listen JSON the rest of the pipeline reads."""

    utterances = _utterances(payload)
    if not utterances:
        raise ValueError("Gladia transcript has no utterances")
    return _deepgram_document(utterances)


def _utterances(payload: str | list | dict) -> list[dict]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ValueError("Gladia payload must be a list of utterances or a result object")
    if isinstance(payload.get("utterances"), list):
        return [item for item in payload["utterances"] if isinstance(item, dict)]
    result = payload.get("result") or {}
    transcription = result.get("transcription") if isinstance(result, dict) else None
    if isinstance(transcription, dict) and isinstance(transcription.get("utterances"), list):
        return [item for item in transcription["utterances"] if isinstance(item, dict)]
    raise ValueError("Gladia payload has no utterances")


def _deepgram_document(utterances: list[dict]) -> dict:
    words: list[dict] = []
    dg_utterances: list[dict] = []
    sentences: list[dict] = []
    texts: list[str] = []
    for index, utterance in enumerate(utterances, start=1):
        text = str(utterance.get("text") or utterance.get("transcript") or "").strip()
        utt_words = []
        for raw in utterance.get("words") or []:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("punctuated_word") or raw.get("word") or "")
            bare, punctuated = _word_pair(token)
            if not bare:
                continue
            word = {
                "word": bare,
                "start": float(raw.get("start") or 0),
                "end": float(raw.get("end") or 0),
                "confidence": raw.get("confidence"),
                "punctuated_word": punctuated,
            }
            utt_words.append(word)
            words.append(word)
        start = float(utterance.get("start") or (utt_words[0]["start"] if utt_words else 0))
        end = float(utterance.get("end") or (utt_words[-1]["end"] if utt_words else start))
        if text:
            texts.append(text)
        sentences.append({"text": text, "start": start, "end": end})
        dg_utterances.append(
            {
                "start": start,
                "end": end,
                "confidence": utterance.get("confidence"),
                "channel": utterance.get("channel") or 0,
                "transcript": text,
                "id": f"utt-{index}",
                "speaker": utterance.get("speaker"),
                "words": utt_words,
            }
        )
    ends = [word["end"] for word in words] + [item["end"] for item in dg_utterances]
    duration = max(ends) if ends else 0.0
    transcript = " ".join(texts)
    return {
        "metadata": {
            "duration": duration,
            "channels": 1,
            "models": ["gladia"],
        },
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": transcript,
                            "words": words,
                            "paragraphs": {
                                "transcript": transcript,
                                "paragraphs": [
                                    {
                                        "sentences": sentences,
                                        "start": sentences[0]["start"] if sentences else 0,
                                        "end": sentences[-1]["end"] if sentences else 0,
                                        "num_words": len(words),
                                    }
                                ],
                            },
                        }
                    ]
                }
            ],
            "utterances": dg_utterances,
        },
    }


def _word_pair(token: str) -> tuple[str, str]:
    punctuated = token.strip()
    bare = punctuated.strip(".,!?;:\"'").lower()
    return bare, punctuated
