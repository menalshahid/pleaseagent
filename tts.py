"""Text-to-speech using Groq API - PRODUCTION HARDENED.
✓ Works reliably from cloud/datacenter environments (no IP blocking)
✓ Urdu text preserved perfectly
✓ Caching for greeting (massive latency reduction)
✓ Error recovery with fallback
"""
import uuid
import os
import re
import logging
import threading

from groq_utils import get_client, get_next_key_index, GROQ_KEYS

logger = logging.getLogger(__name__)
AUDIO_DIR = "static"

# Groq TTS models and voices.
# Urdu uses playai-tts-arabic: Urdu script is derived from Arabic/Perso-Arabic
# script, so Groq's Arabic TTS model handles Urdu phonetics correctly.
_TTS_MODELS = {
    "en": "playai-tts",
    "ur": "playai-tts-arabic",
}

_VOICES = {
    "en": "Fritz-PlayAI",
    "ur": "Ahmad-PlayAI",
}

def _is_urdu_text(text: str) -> bool:
    """Check if text contains Urdu script characters."""
    for char in str(text):
        code = ord(char)
        if 0x0600 <= code <= 0x06FF:
            return True
    return False

def _clean_text_safe(text: str, language: str) -> str:
    """
    Clean text ONLY of metadata markers - preserve everything else.
    NEVER corrupt Urdu diacritics or script.
    """
    t = str(text).strip()

    # Remove ONLY [TOPIC:...] markers
    t = re.sub(r'\[TOPIC:[^\]]*\]\s*', '', t)

    # Remove ONLY PAGE/TOPIC headers at line start
    t = re.sub(r'^(PAGE|TOPIC)\s*:\s*[^\n]*\n?', '', t, flags=re.MULTILINE)

    return t.strip()

def generate_tts(text: str, language: str = "en") -> str | None:
    """
    Generate MP3 from text using Groq TTS API.
    Returns URL path like /static/audio_xxx.mp3.

    PRODUCTION HARDENED:
    Validates input, preserves Urdu text, returns None on failure,
    verifies file exists before returning URL.
    """

    if not text or not str(text).strip():
        return None

    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)

        clean_text = _clean_text_safe(text, language)

        if not clean_text or len(clean_text.strip()) < 2:
            logger.warning("[TTS] Text became empty after cleaning, using original")
            clean_text = str(text).strip()

        if len(clean_text) > 2000:
            logger.warning("[TTS] Text truncated from %d to 1997 chars", len(clean_text))
            clean_text = clean_text[:1997] + "..."

        is_urdu = language == "ur" or _is_urdu_text(clean_text)
        effective_lang = "ur" if is_urdu else "en"

        model = _TTS_MODELS.get(effective_lang, _TTS_MODELS["en"])
        voice = _VOICES.get(effective_lang, _VOICES["en"])
        filename = f"{AUDIO_DIR}/audio_{uuid.uuid4().hex}.mp3"

        logger.info(
            "[TTS] Generating | lang=%s | urdu=%s | model=%s | voice=%s | len=%d | file=%s",
            language, is_urdu, model, voice, len(clean_text), filename
        )

        # All exceptions (network errors, rate limits, invalid credentials, read failures)
        # are caught by the outer try-except which logs and returns None.
        client = get_client(get_next_key_index())
        response = client.audio.speech.create(
            model=model,
            voice=voice,
            input=clean_text,
            response_format="mp3",
        )

        audio_bytes = response.read()
        if not audio_bytes:
            logger.error("[TTS] Groq returned empty audio")
            return None

        with open(filename, "wb") as f:
            f.write(audio_bytes)

        if not os.path.exists(filename):
            logger.error("[TTS] File not created: %s", filename)
            return None

        file_size = os.path.getsize(filename)
        if file_size == 0:
            logger.error("[TTS] File is empty: %s", filename)
            os.remove(filename)
            return None

        url = f"/{filename}".replace("//", "/")
        logger.info("[TTS] Success | %d bytes | %s", file_size, url)
        return url

    except Exception as e:
        logger.exception("[TTS] Error (language=%s): %s", language, str(e)[:100])
        return None


# ── Greeting prefetch cache ───────────────────────────────────────────────────

_greeting_cache = {}

def prefetch_greeting(text: str, language: str = "en") -> None:
    """Call at app startup to generate greeting audio in background."""

    def _gen():
        try:
            url = generate_tts(text, language=language)
            if url:
                _greeting_cache[language] = url
                logger.info("[TTS] Greeting prefetched: %s", url)
        except Exception as e:
            logger.warning("[TTS] Greeting prefetch failed: %s", e)

    thread = threading.Thread(target=_gen, daemon=True)
    thread.start()


def get_cached_greeting(language: str = "en") -> str | None:
    """Get prefetched greeting URL or None."""
    return _greeting_cache.get(language)