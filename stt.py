"""Speech-to-text using Groq Whisper API. Supports webm, mp3, wav, etc. from browser.

Language modes
--------------
language=None  →  auto-detect (language-selection turn only), whisper-large-v3-turbo
language="en"  →  force English, turbo
language="ur"  →  force Urdu, turbo (same model family as English — faster than old large-v3)

Mobile compatibility:
- Accepts webm, mp4, wav, ogg from MediaRecorder
- Urdu prompts and context to improve recognition
"""
import os
import re
import logging

logger = logging.getLogger(__name__)


def transcribe_audio(audio_file, language: str | None = None) -> str:
    """Transcribe audio from browser MediaRecorder (webm/m4a/ogg/wav/mp4).
    language: 'en', 'ur', or None (auto-detect)
    """
    api_key = (
        os.getenv("GROQ_API_KEY")
        or (os.getenv("GROQ_API_KEYS") or "").split(",")[0].strip()
    )
    if not api_key:
        logger.warning("GROQ_API_KEY not set; STT will fail.")
        return "Sorry, speech recognition is not configured."

    try:
        from groq import Groq
        client = Groq(api_key=api_key)

        data = audio_file.read()
        if not data or len(data) < 100:
            return "Sorry, the audio was too short or empty."

        # Use original filename for format detection (important for Safari/Android)
        fn = getattr(audio_file, "filename", None) or "audio.webm"
        if not fn or "." not in fn:
            fn = "audio.webm"

        logger.info("STT [lang=%s, file=%s, size=%d bytes]", language, fn, len(data))

        if language == "en":
            transcription = client.audio.transcriptions.create(
                file=(fn, data),
                model="whisper-large-v3-turbo",
                language="en",
                prompt=(
                    "IST Institute of Space Technology. BS Bachelor of Science. "
                    "Admissions, fee structure, transport, faculty, "
                    "electrical engineering, computer science, "
                    "department, program, engineering, Pakistan."
                ),
            )
        elif language == "ur":
            # Fast turbo model + explicit Urdu (same speed class as English)
            transcription = client.audio.transcriptions.create(
                file=(fn, data),
                model="whisper-large-v3-turbo",
                language="ur",
                prompt=(
                    "IST Institute of Space Technology. "
                    "BS اور MS، fee structure، admissions، transport، faculty۔ "
                    "انجینئرنگ، کمپیوٹر سائنس، الیکٹرانکس، اردو، پاکستان۔"
                ),
            )
        else:
            # Language-selection turn: auto-detect (mixed Urdu + English)
            transcription = client.audio.transcriptions.create(
                file=(fn, data),
                model="whisper-large-v3-turbo",
                prompt=(
                    "IST Institute of Space Technology. English or Urdu. "
                    "BS اور MS پروگرام، fee، admissions۔ انگریزی یا اردو۔"
                ),
            )

        text = (transcription.text if hasattr(transcription, "text") else str(transcription)).strip()

        # ── English-mode post-processing only ────────────────────────────────
        if language == "en":
            if "industry" in text.lower() and (
                "transport" in text.lower() or "offer" in text.lower()
            ):
                text = text.replace("industry", "IST").replace("Industry", "IST")
            if re.search(r"\bP\s*S\b|\bPS\b", text, re.I) and any(
                w in text.lower()
                for w in ["electrical", "mechanical", "computer", "engineering"]
            ):
                text = re.sub(r"\bP\s*S\b", "BS", text, flags=re.I)

        # ── Urdu: PRESERVE all text, minimal post-processing ────────────────
        # Do NOT modify Urdu transcriptions - they're usually correct from Whisper

        logger.info("STT [lang=%s, result]: %s", language, repr(text)[:100])
        return text or "Sorry, I could not understand the audio."

    except Exception as e:
        logger.exception("STT error [lang=%s]: %s", language, e)
        error_msg = str(e).lower()
        
        # Provide specific error messages
        if "authentication" in error_msg or "api_key" in error_msg:
            return "Sorry, the speech service is not configured properly."
        elif "timeout" in error_msg or "connection" in error_msg:
            return "Sorry, the connection timed out. Please try again."
        else:
            return "Sorry, I could not understand the audio. Please try again."