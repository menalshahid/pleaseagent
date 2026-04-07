"""Text-to-speech using edge-tts (free, no API key) - PRODUCTION HARDENED.
✓ Proper sync/async handling for Flask
✓ Urdu text preserved perfectly
✓ Caching for greeting (massive latency reduction)
✓ Error recovery with fallback
"""
import edge_tts
import uuid
import os
import logging
import asyncio
import threading
from functools import lru_cache

logger = logging.getLogger(__name__)
AUDIO_DIR = "static"

_VOICES = {
    "en": "en-US-AriaNeural",
    "ur": "ur-PK-AsadNeural",
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
    import re
    
    t = str(text).strip()
    
    # Remove ONLY [TOPIC:...] markers
    t = re.sub(r'\[TOPIC:[^\]]*\]\s*', '', t)
    
    # Remove ONLY PAGE/TOPIC headers at line start
    t = re.sub(r'^(PAGE|TOPIC)\s*:\s*[^\n]*\n?', '', t, flags=re.MULTILINE)
    
    # For Urdu: NEVER apply aggressive regex that damages script
    # For English: minimal cleanup only
    
    return t.strip()

# Synchronous save helper for Flask
def _get_sync_save():
    """Create sync save function using threading."""
    
    def sync_save(communicate, filename: str, timeout: int = 20):
        result = {"error": None, "done": False}
        
        def _run():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(communicate.save(filename))
                    result["done"] = True
                finally:
                    loop.close()
            except Exception as e:
                result["error"] = e
        
        thread = threading.Thread(target=_run, daemon=False)
        thread.start()
        thread.join(timeout=timeout)
        
        if thread.is_alive():
            logger.error("TTS generation thread timeout after %ds", timeout)
            raise TimeoutError(f"TTS generation timeout ({timeout}s)")
        
        if result["error"]:
            raise result["error"]
        
        if not result["done"]:
            raise RuntimeError("TTS thread finished without saving")
    
    return sync_save

_sync_save = _get_sync_save()

def generate_tts(text: str, language: str = "en") -> str | None:
    """
    Generate MP3 from text. Returns URL path like /static/audio_xxx.mp3.
    
    PRODUCTION HARDENED:
    ✓ Validates input
    ✓ Preserves Urdu text perfectly
    ✓ 20-second timeout
    ✓ Returns None on failure (non-blocking)
    ✓ Verifies file exists before returning URL
    """
    
    if not text or not str(text).strip():
        return None
    
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
        
        # Clean metadata but preserve content
        clean_text = _clean_text_safe(text, language)
        
        if not clean_text or len(clean_text.strip()) < 2:
            logger.warning("[TTS] Text became empty after cleaning, using original")
            clean_text = str(text).strip()
        
        # Truncate only if way too long (preserve content)
        if len(clean_text) > 2000:
            logger.warning("[TTS] Text truncated from %d to 1997 chars", len(clean_text))
            clean_text = clean_text[:1997] + "..."
        
        voice = _VOICES.get(language, _VOICES["en"])
        filename = f"{AUDIO_DIR}/audio_{uuid.uuid4().hex}.mp3"
        
        is_urdu = language == "ur" or _is_urdu_text(clean_text)
        
        logger.info(
            "[TTS] Generating | lang=%s | urdu=%s | len=%d | file=%s",
            language, is_urdu, len(clean_text), filename
        )
        
        # Create Communicate object
        communicate = edge_tts.Communicate(clean_text, voice)
        
        # Save with 20-second timeout
        try:
            _sync_save(communicate, filename, timeout=20)
        except TimeoutError:
            logger.error("[TTS] Timeout after 20s, returning None")
            return None
        
        # Verify file was created and has content
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

# OPTIONAL: Cache greeting to avoid TTS on every call
@lru_cache(maxsize=2)
def _cached_greeting_tts(text_hash: str, language: str) -> str | None:
    """Cache greeting TTS to reduce latency on repeated calls."""
    # In real use, pass hash of text, not full text
    pass

<<<<<<< HEAD
# Prefetch greeting on startup
_greeting_cache = {}

def prefetch_greeting(text: str, language: str = "en") -> None:
    """Call at app startup to generate greeting audio in background."""
    import threading
    
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
=======
# FALLBACK: If asyncio context is problematic, use synchronous wrapper
def _get_sync_save():
    """Create a synchronous save function for edge-tts.
    Runs edge-tts in a background thread with its own event loop so it is
    compatible with gunicorn + gevent.  Exceptions are propagated back to the
    caller so that generate_tts_v2 can detect failures reliably.
    """
    import threading

    def sync_save(communicate, filename):
        result: dict = {"error": None, "done": False}

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(communicate.save(filename))
                result["done"] = True
            except Exception as exc:  # broad catch: edge-tts can raise aiohttp, SSL, OSError, etc.
                result["error"] = exc
            finally:
                loop.close()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=15)  # Wait max 15 seconds

        if thread.is_alive():
            raise TimeoutError("TTS generation timed out after 15 s")
        if result["error"] is not None:
            raise result["error"]
        # Defensive: guard against abnormal thread exit (e.g., OS signal) that
        # neither raised an exception nor reached the success path.
        if not result["done"]:
            raise RuntimeError("TTS thread finished without saving the file")

    return sync_save

# Synchronous save helper used by generate_tts_v2
_sync_save = _get_sync_save()

def generate_tts_v2(text: str, language: str = "en") -> str | None:
    """
    Alternative TTS implementation using threading for Flask compatibility.
    """
    if not text or not str(text).strip():
        return None
    
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
        filename = f"{AUDIO_DIR}/audio_{uuid.uuid4().hex}.mp3"
        voice = _VOICES.get(language, _VOICES["en"])
        
        # Clean text
        if language == "ur":
            clean_text = _clean_urdu_safe(text)
        else:
            clean_text = _clean_english_safe(text)
        
        if not clean_text or len(clean_text.strip()) < 2:
            clean_text = str(text).strip()
        
        if len(clean_text) > 1500:
            clean_text = clean_text[:1497] + "..."
        
        logger.info(
            "TTS [lang=%s, len=%d]: %s...",
            language, len(clean_text),
            clean_text[:60].replace('\n', ' ')
        )
        
        # Use sync wrapper
        communicate = edge_tts.Communicate(clean_text, voice)
        _sync_save(communicate, filename)

        # Verify the file was actually written before returning its URL
        if not os.path.exists(filename) or os.path.getsize(filename) == 0:
            logger.error("TTS file missing or empty after save: %s", filename)
            return None
        
        return f"/{filename}".replace("//", "/")
        
    except Exception as e:
        logger.exception("TTS error: %s", e)
        return None

# Override default function to use v2
original_generate_tts = generate_tts
generate_tts = generate_tts_v2
>>>>>>> 05058c6e75e2dc2106b5c65bfa6148fcd8a4b4e6
