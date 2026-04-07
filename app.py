import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from flask import Flask, render_template, request, jsonify
import rag
from rag import answer_question
from tts import generate_tts
from stt import transcribe_audio

app = Flask(__name__)

# ── Per-call state ────────────────────────────────────────────────────────────
# Keep per-device/session state isolated by call_id to avoid cross-device bleed.

_MAX_HISTORY_TURNS = 10
_DEFAULT_CALL_ID = "default"
_calls: dict[str, dict] = {}

_GREETING_TEXT = (
    "Hello! This is the IST admissions helpline. "
    "Please say English or Urdu to choose your language."
)

# ── Language detection ────────────────────────────────────────────────────────

_URDU_SIGNALS = ["urdu", "اردو", "urdoo", "urdo", "اردو میں", "urdu mein", "urdume"]
_ENGLISH_SIGNALS = ["english", "انگریزی", "eng ", "inglish", "inglis", "in english",
                    "english mein", "english me"]

# Treat underscores as non-meaningful symbols (same as punctuation/noise),
# while preserving all Unicode letters (including Urdu) as meaningful content.
_PUNCT_OR_SYMBOL_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_MAX_ACCIDENTAL_CAPTURE_LENGTH = 4
_NON_QUESTION_STT_SNIPPETS = (
    "you",
    "thank you",
    "thanks for watching",
    "please subscribe",
    "music",
    "background music",
    "applause",
    "clapping",
    "noise",
    "inaudible",
    "silence",
)

def _get_call_id(req, body: dict | None = None) -> str:
    """Extract stable call identifier from request (query/json/form/header)."""
    cid = (
        req.args.get("call_id")
        or (body or {}).get("call_id")
        or req.form.get("call_id")
        or req.headers.get("X-Call-Id")
        or _DEFAULT_CALL_ID
    )
    cid = str(cid).strip()
    if not cid:
        return _DEFAULT_CALL_ID
    # Defensive length bound; keep ASCII/Unicode content as-is.
    return cid[:128]

def _get_call_state(call_id: str) -> dict:
    state = _calls.get(call_id)
    if state is None:
        state = {"history": [], "language": None}
        _calls[call_id] = state
    return state

def _detect_language(text: str) -> str | None:
    """Return 'ur', 'en', or None if choice is unclear."""
    t = text.lower().strip()
    if any(s in t for s in _URDU_SIGNALS):
        return "ur"
    if any(s in t for s in _ENGLISH_SIGNALS):
        return "en"
    return None

def _speak(text: str, lang: str = "en") -> str | None:
    """Sanitize and generate TTS. Returns audio URL or None."""
    t = str(text).strip()

    # Remove metadata markers only
    t = re.sub(r'\[TOPIC:[^\]]*\]\s*', '', t)
    t = re.sub(r'^(PAGE|TOPIC)\s*:\s*[^\n]*\n?', '', t, flags=re.MULTILINE)

    # Safety: truncate if way too long
    if len(t) > 500:
        t = t[:500]

    return generate_tts(t, language=lang)

def _looks_like_noise_or_hallucinated_stt(text: str) -> bool:
    """Best-effort guard to avoid answering non-questions from noisy captures.

    Heuristics:
    - empty/whitespace or punctuation/symbol-only transcripts
    - common filler utterances ("hmm", "umm", etc.)
    - very short accidental latin snippets (length <= 4)
    - known non-question/hallucination fragments observed in noisy audio
    """
    t = (text or "").strip()
    if not t:
        return True
    if _PUNCT_OR_SYMBOL_ONLY_RE.match(t):
        return True

    t_lower = t.lower()
    if t_lower in {"hmm", "hmmm", "umm", "uh", "uhh", "huh", "ok", "okay"}:
        return True

    # Very short non-language snippets are usually accidental captures.
    if 1 <= len(t_lower) <= _MAX_ACCIDENTAL_CAPTURE_LENGTH and re.fullmatch(r"[a-z]+", t_lower):
        return True

    return any(snippet in t_lower for snippet in _NON_QUESTION_STT_SNIPPETS)

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/greeting")
def greeting():
    """Return greeting TTS that asks for language selection.
    Played once per call; asks user to say English or Urdu.
    Returns both the audio URL and the greeting text so the frontend can
    display the text even when audio playback is unavailable.
    """
    audio = generate_tts(_GREETING_TEXT, language="en")
    return jsonify({"audio": audio or "", "text": _GREETING_TEXT})


@app.route("/api/call/end", methods=["POST"])
def call_end():
    """Reset per-call state and clean up old TTS audio files."""
    import glob
    call_id = _get_call_id(request, request.get_json(silent=True) or {})
    _calls.pop(call_id, None)

    # Cleanup old audio files
    try:
        import time
        now = time.time()
        for path in glob.glob("static/audio_*.mp3"):
            if now - os.path.getmtime(path) > 3600:  # older than 1 hour
                try:
                    os.remove(path)
                except OSError:
                    pass
    except Exception:
        pass

    return jsonify({"ok": True})


@app.route("/api/call/audio", methods=["POST"])
def call_audio():
    """Audio in → transcript → reply → TTS out.
    First turn after greeting: language selection.
    Subsequent turns: normal Q&A in the chosen language.
    """
    # ── Transcription ─────────────────────────────────────────────────────────
    transcript = ""
    body = request.get_json(silent=True) or {} if request.is_json else {}
    call_id = _get_call_id(request, body)
    call_state = _get_call_state(call_id)
    call_history = call_state["history"]
    call_language = call_state["language"]

    if request.is_json:
        transcript = (body.get("text") or "").strip()
        if not transcript:
            return jsonify({"error": "No text"}), 400
    else:
        if "audio" not in request.files:
            return jsonify({"error": "No audio"}), 400

        audio_file = request.files["audio"]
        if audio_file.filename == "":
            return jsonify({"error": "Empty audio"}), 400

        # English → forced en. Urdu → forced ur (fast turbo). First turn → auto-detect.
        if call_language == "en":
            stt_lang = "en"
        elif call_language == "ur":
            stt_lang = "ur"
        else:
            stt_lang = None  # auto-detect

        transcript = transcribe_audio(audio_file, language=stt_lang)

    # Graceful handling of empty/failed STT
    if not transcript or "sorry" in transcript.lower():
        return jsonify({
            "transcript": transcript or "",
            "reply": "",
            "audio": "",
            "end_call": False
        })

    # ── Noise guard ───────────────────────────────────────────────────────────
    # On language-selection turn, allow short tokens like "Urdu"/"English" before noise guard.
    if call_language is None:
        prechosen = _detect_language(transcript)
    else:
        prechosen = None

    if _looks_like_noise_or_hallucinated_stt(transcript):
        if prechosen not in {"ur", "en"}:
            reprompt = (
                "معذرت، آواز واضح نہیں آئی۔ براہ کرم سوال دوبارہ واضح طور پر پوچھیں۔"
                if call_language == "ur"
                else "Sorry, I could not hear a clear question. Please ask again."
            )
            audio_url = _speak(reprompt, call_language or "en")
            return jsonify({
                "transcript": transcript,
                "reply": reprompt,
                "audio": audio_url or "",
                "end_call": False,
            })

    # ── Language selection turn ───────────────────────────────────────────────
    if call_language is None:
        chosen = prechosen or _detect_language(transcript)

        if chosen == "ur":
            call_state["language"] = "ur"
            reply = "بہت اچھا! میں اب اردو میں آپ کی مدد کروں گی۔ آپ کا سوال کیا ہے؟"
            audio_url = _speak(reply, "ur")
            return jsonify({
                "transcript": transcript,
                "reply": reply,
                "audio": audio_url or "",
                "end_call": False,
            })

        if chosen == "en":
            call_state["language"] = "en"
            reply = "Great! I will assist you in English. What is your query?"
            audio_url = _speak(reply, "en")
            return jsonify({
                "transcript": transcript,
                "reply": reply,
                "audio": audio_url or "",
                "end_call": False,
            })

        # Could not detect language
        reply = (
            "I'm sorry, I didn't catch that. "
            "Please say English or Urdu. براہ کرم English یا Urdu کہیں۔"
        )
        audio_url = _speak(reply, "en")
        return jsonify({
            "transcript": transcript,
            "reply": reply,
            "audio": audio_url or "",
            "end_call": False,
        })

    # ── Normal Q&A turn ───────────────────────────────────────────────────────
    lang = call_state["language"]  # "en" or "ur"
    kind, response = answer_question(transcript, history=list(call_history), language=lang)

    if response:
        call_history.append({"role": "user",      "content": transcript})
        call_history.append({"role": "assistant",  "content": response})
        if len(call_history) > _MAX_HISTORY_TURNS * 2:
            call_history[:] = call_history[-(_MAX_HISTORY_TURNS * 2):]
        audio_url = _speak(response, lang)
    else:
        audio_url = None

    if kind == "__END_CALL__":
        _calls.pop(call_id, None)

    return jsonify({
        "transcript": transcript,
        "reply": response or "",
        "audio": audio_url or "",
        "end_call": kind == "__END_CALL__",
    })

@app.route("/api/admin/reload-kb", methods=["POST"])
def admin_reload_kb():
    """Reload knowledge base after updates."""
    secret = os.environ.get("IST_ADMIN_SECRET") or os.environ.get("KB_RELOAD_SECRET")
    if not secret:
        return jsonify({"error": "Secret not configured"}), 503
    if request.headers.get("X-Admin-Secret") != secret:
        return jsonify({"error": "Unauthorized"}), 401

    rag.reload_kb()
    return jsonify({"ok": True, "chunks": len(rag.chunks)})

@app.errorhandler(500)
def handle_500(error):
    import traceback
    traceback.print_exc()
    return jsonify({"error": "Server error. Please try again."}), 500

@app.errorhandler(404)
def handle_404(error):
    return jsonify({"error": "Not found"}), 404

if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes"},
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
    )