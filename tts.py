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

from groq import BadRequestError as GroqBadRequestError, NotFoundError as GroqNotFoundError
from groq_utils import get_client, get_next_key_index, GROQ_KEYS

logger = logging.getLogger(__name__)
# Use absolute path so audio files are always written to the right directory
# regardless of the working directory when gunicorn starts on Render.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(_APP_DIR, "static")

# Groq TTS models and voices.
# playai-tts / playai-tts-arabic were decommissioned 2025-12-23.
# Replacement: Orpheus TTS (canopylabs/orpheus-v1-*).
# Urdu uses orpheus-v1-arabic: Urdu script is derived from Arabic/Perso-Arabic
# script, so Groq's Arabic TTS model handles Urdu phonetics correctly.
_TTS_MODELS = {
    "en": "canopylabs/orpheus-v1-english",
    "ur": "canopylabs/orpheus-v1-arabic",
}

_VOICES = {
    "en": "leo",
    "ur": "jad",
}

_GROQ_TTS_DISABLED: dict[str, str] = {}
_GROQ_TTS_DISABLE_LOCK = threading.Lock()

def _disable_groq_tts(language: str, reason: str) -> None:
    """Disable Groq TTS for a language after non-retryable errors."""
    with _GROQ_TTS_DISABLE_LOCK:
        if language in _GROQ_TTS_DISABLED:
            return
        _GROQ_TTS_DISABLED[language] = reason
    logger.warning("[TTS] Groq TTS disabled for lang=%s: %s", language, reason)

def _get_groq_tts_disable_reason(language: str) -> str | None:
    return _GROQ_TTS_DISABLED.get(language)

def _should_disable_groq_tts(err: Exception) -> str | None:
    message = str(err).lower()
    if "requires terms acceptance" in message or "accept the terms" in message:
        return "terms acceptance required in Groq console"
    if "model_not_found" in message or "does not exist" in message or "do not have access" in message:
        return "model not available for this API key"
    return None

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

def _gtts_fallback(text: str, effective_lang: str, filename: str) -> str | None:
    """Generate MP3 via Google TTS (gTTS) as fallback. Returns URL or None."""
    try:
        from gtts import gTTS  # imported here so Groq-only installs still work

        gtts_lang = "ar" if effective_lang == "ur" else "en"
        tts_obj = gTTS(text=text, lang=gtts_lang)
        tts_obj.save(filename)

        if not os.path.exists(filename) or os.path.getsize(filename) == 0:
            logger.error("[TTS] gTTS produced empty file: %s", filename)
            return None

        url = "/static/" + os.path.basename(filename)
        logger.info("[TTS] gTTS fallback success | %s", url)
        return url
    except Exception as e:
        logger.exception("[TTS] gTTS fallback failed: %s", e)
        return None


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
        filename = os.path.join(AUDIO_DIR, f"audio_{uuid.uuid4().hex}.mp3")

        disable_reason = _get_groq_tts_disable_reason(effective_lang)
        if disable_reason:
            logger.info(
                "[TTS] Groq TTS disabled for lang=%s (%s); using gTTS",
                effective_lang,
                disable_reason,
            )
            return _gtts_fallback(clean_text, effective_lang, filename)

        if not GROQ_KEYS:
            logger.warning("[TTS] GROQ_API_KEY(S) not configured; using gTTS fallback")
            return _gtts_fallback(clean_text, effective_lang, filename)

        logger.info(
            "[TTS] Generating | lang=%s | urdu=%s | model=%s | voice=%s | len=%d | file=%s",
            language, is_urdu, model, voice, len(clean_text), filename
        )

        # All exceptions (network errors, rate limits, invalid credentials, read failures)
        # are caught by the outer try-except which logs and returns None.
        client = get_client(get_next_key_index())
        try:
            response = client.audio.speech.create(
                model=model,
                voice=voice,
                input=clean_text,
                response_format="mp3",
            )
            audio_bytes = response.read()
        except (GroqBadRequestError, GroqNotFoundError) as groq_err:
            logger.warning(
                "[TTS] Groq error (%s) for lang=%s, falling back to gTTS: %s",
                type(groq_err).__name__, language, str(groq_err)[:200],
            )
            disable_reason = _should_disable_groq_tts(groq_err)
            if disable_reason:
                _disable_groq_tts(effective_lang, disable_reason)
            return _gtts_fallback(clean_text, effective_lang, filename)

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

        url = "/static/" + os.path.basename(filename)
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
