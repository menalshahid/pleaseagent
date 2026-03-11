"""Speech-to-text using Groq Whisper API. Supports webm, mp3, wav, etc. from browser."""
import os
import re
import logging

logger = logging.getLogger(__name__)

def transcribe_audio(audio_file):
    """Transcribe audio file (from browser MediaRecorder - webm, etc.) using Groq Whisper."""
    api_key = os.getenv("GROQ_API_KEY") or (os.getenv("GROQ_API_KEYS") or "").split(",")[0].strip()
    if not api_key:
        logger.warning("GROQ_API_KEY not set; STT will fail. Set it in .env")
        return "Sorry, speech recognition is not configured. Please type your question."

    try:
        from groq import Groq
        client = Groq(api_key=api_key)

        data = audio_file.read()
        if not data or len(data) < 100:
            return "Sorry, the audio was too short or empty."

        # Use original filename for Groq (webm/m4a/ogg) — important for Safari/Android
        fn = getattr(audio_file, "filename", None) or "audio.webm"
        if not fn or fn == "":
            fn = "audio.webm"
        if "." not in fn:
            fn = "audio.webm"

        # language="en" + prompt to bias IST, BS, admissions terms
        transcription = client.audio.transcriptions.create(
            file=(fn, data),
            model="whisper-large-v3-turbo",
            language="en",
            prompt="IST Institute of Space Technology. BS Bachelor of Science. Admissions, fee structure, transport, faculty, electrical engineering.",
        )
        text = transcription.text if hasattr(transcription, "text") else str(transcription)
        text = text.strip()
        # Fix common STT errors
        if "industry" in text.lower() and ("transport" in text.lower() or "offer" in text.lower()):
            text = text.replace("industry", "IST").replace("Industry", "IST")
        if re.search(r"\bP\s*S\b|\bPS\b", text, re.I) and any(w in text.lower() for w in ["electrical", "mechanical", "computer", "engineering"]):
            text = re.sub(r"\bP\s*S\b", "BS", text, flags=re.I).replace("PS ", "BS ")
        logger.info("Transcription result: %s", repr(text)[:80])
        return text or "Sorry, I could not understand the audio."

    except Exception as e:
        logger.exception("STT error: %s", e)
        return "Sorry, I could not understand the audio."
