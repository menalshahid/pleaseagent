"""Text-to-speech using edge-tts (faster than gTTS). Returns URL path for browser playback."""
import edge_tts
import uuid
import os
import logging

logger = logging.getLogger(__name__)
AUDIO_DIR = "static"
VOICE = "en-US-AriaNeural"

def generate_tts(text):
    """Generate MP3 from text. Returns path like /static/audio_xxx.mp3 for browser."""
    if not text or not str(text).strip():
        return None
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
        filename = f"{AUDIO_DIR}/audio_{uuid.uuid4().hex}.mp3"
        communicate = edge_tts.Communicate(str(text).strip(), VOICE)
        communicate.save_sync(filename)
        return f"/{filename}".replace("//", "/")
    except Exception as e:
        logger.exception("TTS error: %s", e)
        return None