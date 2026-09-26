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

    def sentence_windows(self) -> list[tuple[float, float]]:
        """Sentence ranges used to keep sound effects to one per sentence.

        Word punctuation is preferred, because a Deepgram utterance can hold
        more than one sentence. Segments are the fallback, then the whole story.
        """

        from_words = _windows_from_words(self.words)
        if from_words:
            return from_words
        from_segments = [
            (segment.start, segment.end)
            for segment in self.segments
            if segment.end > segment.start
        ]
        if from_segments:
            return from_segments
        if self.duration_seconds > 0:
            return [(0.0, self.duration_seconds)]
        return []


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


def _windows_from_words(words: list[DeepgramWord]) -> list[tuple[float, float]]:
    if not words:
        return []
    if not any(_ends_sentence(word.display) for word in words):
        return []
    windows: list[tuple[float, float]] = []
    start: float | None = words[0].start
    for word in words:
        if start is None:
            start = word.start
        if _ends_sentence(word.display):
            windows.append((start, word.end))
            start = None
    if start is not None:
        windows.append((start, words[-1].end))
    return [(begin, end) for begin, end in windows if end > begin]


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip("\"'”’")
    return bool(stripped) and stripped[-1] in ".!?"


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
