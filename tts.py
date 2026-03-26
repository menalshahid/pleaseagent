"""Text-to-speech using edge-tts (free, no API key).
English : en-US-AriaNeural
Urdu    : ur-PK-AsadNeural  (most reliable for Urdu text preservation)

Key fixes for Urdu:
1. NEVER strip or modify Urdu text before TTS
2. Keep all Unicode/diacritics intact
3. Preserve names and proper nouns
4. Avoid aggressive regex that damages Urdu script
"""
import edge_tts
import uuid
import os
import logging
import asyncio

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
        # Urdu is in Unicode range U+0600-U+06FF
        if 0x0600 <= code <= 0x06FF:
            return True
    return False

def _clean_urdu_safe(text: str) -> str:
    """
    Clean Urdu text WITHOUT damaging special characters.
    Only remove:
    - Metadata markers like [TOPIC:...] (wrapped in brackets)
    - PAGE: and TOPIC: headers at line starts
    Do NOT remove Urdu diacritics, ligatures, or common words
    """
    import re
    
    t = str(text).strip()
    
    # Remove [TOPIC:...] markers (but preserve content after them)
    t = re.sub(r'\[TOPIC:[^\]]*\]\s*', '', t)
    
    # Remove PAGE: and TOPIC: headers only at line start
    t = re.sub(r'^(PAGE|TOPIC)\s*:\s*[^\n]*\n?', '', t, flags=re.MULTILINE)
    
    # DON'T use aggressive cleaning that would damage Urdu
    # The following are REMOVED to prevent damage:
    # - t = re.sub(r'[^\w\s۔؟]', '', t)  ❌ WRONG - kills diacritics
    # - removing all non-ASCII ❌ WRONG - kills entire Urdu script
    
    return t.strip()

def _clean_english_safe(text: str) -> str:
    """Clean English text - can be more aggressive."""
    import re
    
    t = str(text).strip()
    t = re.sub(r'\[TOPIC:[^\]]*\]\s*', '', t)
    t = re.sub(r'^(PAGE|TOPIC)\s*:\s*[^\n]*\n?', '', t, flags=re.MULTILINE)
    
    return t.strip()

def generate_tts(text: str, language: str = "en") -> str | None:
    """
    Generate MP3 from text. Returns URL path like /static/audio_xxx.mp3.
    language: 'en' or 'ur'
    
    Urdu handling: Preserves ALL Urdu characters, names, diacritics.
    """
    if not text or not str(text).strip():
        return None
    
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
        filename = f"{AUDIO_DIR}/audio_{uuid.uuid4().hex}.mp3"
        voice = _VOICES.get(language, _VOICES["en"])
        
        # Clean text ONLY of metadata markers, preserve all script
        if language == "ur":
            clean_text = _clean_urdu_safe(text)
            is_urdu = True
        else:
            clean_text = _clean_english_safe(text)
            is_urdu = False
        
        # Sanity check: if text becomes empty after cleaning, use original
        if not clean_text or len(clean_text.strip()) < 2:
            clean_text = str(text).strip()
        
        # Max length safety (but preserve content)
        if len(clean_text) > 1500:
            clean_text = clean_text[:1497] + "..."
        
        logger.info(
            "TTS [lang=%s, urdu_detected=%s, len=%d]: %s...",
            language, is_urdu, len(clean_text),
            clean_text[:60].replace('\n', ' ')
        )
        
        # Use asyncio to handle edge-tts async API
        communicate = edge_tts.Communicate(clean_text, voice)
        
        # Run async save in a new event loop or use existing one
        try:
            # Try to use existing event loop if in async context
            loop = asyncio.get_running_loop()
            # If we got here, we're in async context - create task
            asyncio.create_task(_save_tts_async(communicate, filename))
            # This won't work - need sync version
        except RuntimeError:
            # No running loop, create new one
            asyncio.run(_save_tts_async(communicate, filename))
        
        url = f"/{filename}".replace("//", "/")
        logger.info("TTS saved: %s", url)
        return url
        
    except Exception as e:
        logger.exception("TTS error [lang=%s]: %s", language, e)
        return None

async def _save_tts_async(communicate, filename: str):
    """Async helper to save TTS."""
    await communicate.save(filename)

# FALLBACK: If asyncio context is problematic, use synchronous wrapper
def _get_sync_save():
    """Create a synchronous save function for edge-tts."""
    import concurrent.futures
    import threading
    
    def sync_save(communicate, filename):
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(communicate.save(filename))
            finally:
                loop.close()
        
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=15)  # Wait max 15 seconds
    
    return sync_save

# Monkey-patch save method to work synchronously in Flask context
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
        
        return f"/{filename}".replace("//", "/")
        
    except Exception as e:
        logger.exception("TTS error: %s", e)
        return None

# Override default function to use v2
original_generate_tts = generate_tts
generate_tts = generate_tts_v2