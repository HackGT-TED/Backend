"""Deepgram prerecorded JSON, plus a simplified transcript shape.

The v1 API does not call Deepgram. Grandparent clients upload the JSON Deepgram
already returned. Unknown Deepgram fields are ignored so real responses validate.

Accepted shapes:

* A prerecorded response with ``metadata``, ``results.channels[].alternatives[]``,
  optional ``results.utterances``, and optional paragraph sentences.
* A small payload with ``transcript``, ``words`` (``word``/``start``/``end``),
  and optional ``segments`` or ``utterances``.
"""

from pydantic import BaseModel, ConfigDict, Field


class DeepgramWord(BaseModel):
    """One word timestamp. ``start`` and ``end`` are seconds from the audio start."""

    model_config = ConfigDict(extra="allow")

    word: str
    start: float
    end: float
    confidence: float | None = None
    punctuated_word: str | None = None
    speaker: int | None = None

    @property
    def display(self) -> str:
        return self.punctuated_word or self.word


class TranscriptSegment(BaseModel):
    """A sentence or utterance window on the story timeline."""

    model_config = ConfigDict(extra="allow")

    text: str
    start: float
    end: float


class DeepgramSentence(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str
    start: float
    end: float


class DeepgramParagraph(BaseModel):
    model_config = ConfigDict(extra="allow")

    sentences: list[DeepgramSentence] = Field(default_factory=list)
    start: float | None = None
    end: float | None = None
    speaker: int | None = None
    num_words: int | None = None


class DeepgramParagraphs(BaseModel):
    model_config = ConfigDict(extra="allow")

    transcript: str | None = None
    paragraphs: list[DeepgramParagraph] = Field(default_factory=list)


class DeepgramAlternative(BaseModel):
    model_config = ConfigDict(extra="allow")

    transcript: str = ""
    confidence: float | None = None
    words: list[DeepgramWord] = Field(default_factory=list)
    paragraphs: DeepgramParagraphs | None = None


class DeepgramChannel(BaseModel):
    model_config = ConfigDict(extra="allow")

    alternatives: list[DeepgramAlternative] = Field(default_factory=list)


class DeepgramUtterance(BaseModel):
    model_config = ConfigDict(extra="allow")

    start: float
    end: float
    transcript: str = ""
    confidence: float | None = None
    words: list[DeepgramWord] = Field(default_factory=list)
    speaker: int | None = None
    id: str | None = None


class DeepgramResults(BaseModel):
    model_config = ConfigDict(extra="allow")

    channels: list[DeepgramChannel] = Field(default_factory=list)
    utterances: list[DeepgramUtterance] | None = None


class DeepgramMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str | None = None
    duration: float | None = None
    channels: int | None = None
    created: str | None = None


class NormalizedTranscript(BaseModel):
    """Transcript fields the sound planner and mixer actually use."""

    text: str
    words: list[DeepgramWord]
    segments: list[TranscriptSegment]
    duration_seconds: float

    def prompt_payload(self) -> dict:
        return {
            "transcript": self.text,
            "duration_seconds": self.duration_seconds,
            "words": [
                {"word": word.display, "start": word.start, "end": word.end}
                for word in self.words
            ],
            "segments": [segment.model_dump() for segment in self.segments],
        }

    def pause_gaps(self, min_gap_ms: int = 600) -> list[dict]:
        """Word gaps and trailing silence at least ``min_gap_ms`` long.

        These are the pauses the planner may leave quiet or fill with a bed.
        Shorter gaps stay out of the prompt.
        """

        min_gap = max(int(min_gap_ms), 0) / 1000
        words = sorted(self.words, key=lambda word: (word.start, word.end))
        gaps: list[dict] = []
        for previous, following in zip(words, words[1:]):
            gap = following.start - previous.end
            if gap + 1e-6 >= min_gap:
                gaps.append(
                    _pause_gap(previous.end, following.start, previous.display, following.display)
                )
        if words:
            tail = self.duration_seconds - words[-1].end
            if tail + 1e-6 >= min_gap:
                gaps.append(_pause_gap(words[-1].end, self.duration_seconds, words[-1].display, ""))
        return gaps


class DeepgramTranscript(BaseModel):
    """Full Deepgram response or a simplified transcript document."""

    model_config = ConfigDict(extra="allow")

    metadata: DeepgramMetadata | None = None
    results: DeepgramResults | None = None
    transcript: str | None = None
    words: list[DeepgramWord] | None = None
    segments: list[TranscriptSegment] | None = None
    utterances: list[DeepgramUtterance] | None = None
    duration: float | None = None

    def normalized(self) -> NormalizedTranscript:
        """Collapse whichever shape arrived into text, words, segments, and duration."""

        alternative = _best_alternative(self.results)
        if alternative is not None:
            text = alternative.transcript or ""
            words = list(alternative.words)
            segments = _segments_from_results(self.results, alternative)
            metadata_duration = self.metadata.duration if self.metadata else None
        else:
            text = self.transcript or ""
            words = list(self.words or [])
            segments = list(self.segments or [])
            if not segments and self.utterances:
                segments = [
                    TranscriptSegment(text=item.transcript, start=item.start, end=item.end)
                    for item in self.utterances
                ]
            metadata_duration = self.duration
            if metadata_duration is None and self.metadata is not None:
                metadata_duration = self.metadata.duration

        if not text and words:
            text = " ".join(word.display for word in words)
        text = text.strip()
        if not text and not words:
            raise ValueError("Transcript is empty")

        return NormalizedTranscript(
            text=text,
            words=words,
            segments=segments,
            duration_seconds=_duration_seconds(metadata_duration, words, segments),
        )


def _best_alternative(results: DeepgramResults | None) -> DeepgramAlternative | None:
    if results is None:
        return None
    for channel in results.channels:
        if channel.alternatives:
            return channel.alternatives[0]
    return None


def _segments_from_results(
    results: DeepgramResults | None,
    alternative: DeepgramAlternative,
) -> list[TranscriptSegment]:
    if results is not None and results.utterances:
        return [
            TranscriptSegment(text=item.transcript, start=item.start, end=item.end)
            for item in results.utterances
        ]
    if alternative.paragraphs is None:
        return []
    segments: list[TranscriptSegment] = []
    for paragraph in alternative.paragraphs.paragraphs:
        for sentence in paragraph.sentences:
            segments.append(
                TranscriptSegment(text=sentence.text, start=sentence.start, end=sentence.end)
            )
    return segments


def _pause_gap(start: float, end: float, after: str, before: str) -> dict:
    return {
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "gap_ms": int(round((end - start) * 1000)),
        "after": after,
        "before": before,
    }


def _duration_seconds(
    metadata_duration: float | None,
    words: list[DeepgramWord],
    segments: list[TranscriptSegment],
) -> float:
    ends = [word.end for word in words] + [segment.end for segment in segments]
    inferred = max(ends) if ends else 0.0
    if metadata_duration is None:
        return inferred
    return max(float(metadata_duration), inferred)
