import re
from flask import Flask, render_template, request, jsonify
from rag import answer_question
from tts import generate_tts
from stt import transcribe_audio

app = Flask(__name__)

_greeting_audio = None

# Per-call conversation history.
# Stored as a flat list of {"role": ..., "content": ...} dicts.
# Reset when /api/call/end is called.
# Keeps the last 10 turns max so the context window doesn't grow unbounded.
_MAX_HISTORY_TURNS = 10
_call_history: list[dict] = []


@app.route("/api/greeting")
def greeting():
    """Return greeting TTS audio path. Cached after first generation."""
    global _greeting_audio
    if _greeting_audio is None:
        _greeting_audio = generate_tts(
            "Hello, this is Institute of Space Technology. What is your query?"
        )
    return jsonify({"audio": _greeting_audio or ""})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/call/end", methods=["POST"])
def call_end():
    """Delete all TTS audio files, reset greeting and conversation history."""
    import glob
    import os
    global _greeting_audio, _call_history
    _greeting_audio = None
    _call_history = []
    for path in glob.glob("static/audio_*.mp3"):
        try:
            os.remove(path)
        except OSError:
            pass
    return jsonify({"ok": True})


@app.route("/api/call/process", methods=["POST"])
def call_process():
    """Single endpoint: audio in → transcript, reply, TTS out."""
    global _call_history

    if "audio" not in request.files:
        return jsonify({"error": "No audio"}), 400

    audio_file = request.files["audio"]
    if audio_file.filename == "":
        return jsonify({"error": "Empty audio"}), 400

    transcript = transcribe_audio(audio_file)
    if not transcript or "sorry" in transcript.lower():
        return jsonify({
            "transcript": transcript or "",
            "reply": "",
            "audio": "",
            "end_call": False,
        })

    # Pass conversation history so the LLM can handle follow-up questions
    kind, response = answer_question(transcript, history=list(_call_history))

    if response:
        # Update history with this exchange
        _call_history.append({"role": "user",      "content": transcript})
        _call_history.append({"role": "assistant",  "content": response})
        # Cap history length to avoid unbounded growth
        if len(_call_history) > _MAX_HISTORY_TURNS * 2:
            _call_history = _call_history[-(  _MAX_HISTORY_TURNS * 2):]

        # Sanitize for TTS: strip markers, truncate long replies
        clean = re.sub(r"\[TOPIC:[^\]]*\]\s*", "", response).strip()
        clean = re.sub(r"(PAGE|TOPIC)\s*:\s*[^\n]*", "", clean).strip()
        if len(clean) > 500:
            clean = clean[:497] + "..."
        audio_path = generate_tts(clean)
    else:
        audio_path = None

    # Reset history when the call ends
    if kind == "__END_CALL__":
        _call_history = []

    return jsonify({
        "transcript": transcript,
        "reply": response,
        "audio": audio_path or "",
        "end_call": kind == "__END_CALL__",
    })


if __name__ == "__main__":
    app.run(debug=True)