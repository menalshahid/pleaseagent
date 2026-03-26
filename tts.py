"""Text-to-speech using edge-tts (free, no API key).
English : en-US-AriaNeural
Urdu    : ur-PK-AsadNeural  (GulNeural often returns "No audio" from Edge; Asad is reliable)
"""
import edge_tts
import uuid
import os
import logging

logger = logging.getLogger(__name__)
AUDIO_DIR = "static"

_VOICES = {
    "en": "en-US-AriaNeural",
    "ur": "ur-PK-AsadNeural",
}

def generate_tts(text: str, language: str = "en") -> str | None:
    """Generate MP3 from text. Returns URL path like /static/audio_xxx.mp3.
    language: 'en' or 'ur'
    """
    if not text or not str(text).strip():
        return None
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
        filename = f"{AUDIO_DIR}/audio_{uuid.uuid4().hex}.mp3"
        voice = _VOICES.get(language, _VOICES["en"])
        communicate = edge_tts.Communicate(str(text).strip(), voice)
        communicate.save_sync(filename)
        return f"/{filename}".replace("//", "/")
    except Exception as e:
        logger.exception("TTS error: %s", e)
        return None