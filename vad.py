"""Server-side Voice Activity Detection (VAD).

Determines whether an audio buffer likely contains speech before sending it to
the Whisper API.  This saves API quota and avoids false transcriptions caused
by silence-only recordings or short noise bursts.

Detection strategy (applied in order):
  1. Minimum size check  – very short buffers are almost certainly silence.
  2. WAV PCM RMS         – accurate energy measurement for WAV files.
  3. Byte-variance proxy – works for compressed formats (webm, mp4, ogg).

Conservative design: defaults to True (speech present) when the result is
ambiguous so that real questions are never silently discarded.
"""
from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)

# ── Tunable thresholds ────────────────────────────────────────────────────────

# Minimum audio payload size — anything below this is almost certainly an
# accidental tap, empty buffer, or pure silence (webm/opus silence: ~1–3 KB).
_MIN_SPEECH_BYTES: int = 2_000

# RMS fraction of int16 max (32767).  0.008 ≈ 262 / 32767 — quiet but real.
_WAV_RMS_SPEECH: float = 0.008

# Mean absolute deviation of sampled payload bytes.
# Compressed silence (webm/ogg/mp4) has near-zero variance; speech is higher.
_BYTE_VARIANCE_SPEECH: float = 12.0


# ── Public API ────────────────────────────────────────────────────────────────

def has_speech(audio_bytes: bytes, mime_hint: str = "") -> bool:
    """Return True if *audio_bytes* likely contain speech.

    Parameters
    ----------
    audio_bytes:
        Raw bytes as received from the browser MediaRecorder.
    mime_hint:
        MIME type string (e.g. ``"audio/wav"`` or ``"audio/webm;codecs=opus"``).
        Used to select the most accurate analysis strategy.

    Returns
    -------
    bool
        ``True``  → process with STT.
        ``False`` → discard as silence / noise.
    """
    if not audio_bytes:
        return False

    n = len(audio_bytes)
    if n < _MIN_SPEECH_BYTES:
        logger.debug("[VAD] Too short (%d B) → silence", n)
        return False

    # WAV: decode PCM samples for precise RMS energy measurement.
    is_wav = (
        "wav" in mime_hint.lower()
        or audio_bytes[:4] == b"RIFF"
        or audio_bytes[:4] == b"RIFX"
    )
    if is_wav:
        try:
            result = _wav_rms(audio_bytes)
            logger.debug("[VAD] WAV RMS → speech=%s", result)
            return result
        except Exception as exc:  # pragma: no cover
            logger.debug("[VAD] WAV decode failed (%s) → byte proxy", exc)

    # Compressed audio (webm / mp4 / ogg): fall back to byte-variance proxy.
    result = _byte_variance(audio_bytes)
    logger.debug("[VAD] Byte variance → speech=%s", result)
    return result


# ── Private helpers ───────────────────────────────────────────────────────────

def _wav_rms(data: bytes) -> bool:
    """Compute RMS of 16-bit PCM WAV samples and compare to threshold."""
    little_endian = data[:4] == b"RIFF"
    int_fmt = "<I" if little_endian else ">I"
    short_pfx = "<" if little_endian else ">"

    pos = 12  # skip RIFF(4) + file-size(4) + WAVE(4)
    while pos + 8 <= len(data):
        chunk_id = data[pos: pos + 4]
        chunk_size = struct.unpack_from(int_fmt, data, pos + 4)[0]
        data_start = pos + 8

        if chunk_id == b"data":
            samples_bytes = data[data_start: data_start + chunk_size]
            n_samples = len(samples_bytes) // 2
            if n_samples == 0:
                return False
            # Unpack all 16-bit signed samples at once.
            samples = struct.unpack_from(
                f"{short_pfx}{n_samples}h", samples_bytes
            )
            rms = (sum(s * s for s in samples) / n_samples) ** 0.5 / 32767.0
            logger.debug("[VAD] WAV RMS=%.5f threshold=%.5f", rms, _WAV_RMS_SPEECH)
            return rms >= _WAV_RMS_SPEECH

        # Move to next chunk (chunks are word-aligned).
        pos = data_start + chunk_size + (chunk_size & 1)

    # No 'data' chunk found – fall back.
    return _byte_variance(data)


def _byte_variance(data: bytes) -> bool:
    """Mean absolute deviation of sampled payload bytes.

    Compressed silence produces very repetitive byte patterns (low variance),
    while compressed speech produces higher byte variance.  We skip the first
    1 KB (container header) and sample every 8th byte for speed.
    """
    header_skip = min(1_024, len(data) // 4)
    payload = data[header_skip:]

    if len(payload) < 256:
        # Too little payload to decide – assume speech to be safe.
        return True

    sampled = payload[::8]
    mean = sum(sampled) / len(sampled)
    mad = sum(abs(b - mean) for b in sampled) / len(sampled)
    logger.debug(
        "[VAD] Byte MAD=%.2f threshold=%.2f n_samples=%d",
        mad, _BYTE_VARIANCE_SPEECH, len(sampled),
    )
    return mad >= _BYTE_VARIANCE_SPEECH
