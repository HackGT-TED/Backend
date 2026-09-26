"""Tiny in-memory WAV clips for tests. No ffmpeg required to build them."""

import io
import math
import struct
import wave


def sine_wav_bytes(
    duration_ms: int,
    frequency: float = 440.0,
    sample_rate: int = 44100,
    amplitude: float = 0.5,
) -> bytes:
    sample_count = int(sample_rate * duration_ms / 1000)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = bytearray()
        for index in range(sample_count):
            sample = int(
                amplitude * 32767 * math.sin(2 * math.pi * frequency * index / sample_rate)
            )
            frames += struct.pack("<h", sample)
        handle.writeframes(frames)
    return buffer.getvalue()
