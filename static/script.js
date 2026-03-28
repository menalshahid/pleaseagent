/**
 * IST Voice Assistant - Mobile-optimized audio recording & playback
 * iOS Safari fixes:
 *  1. Removed crossOrigin="anonymous" (breaks iOS audio playback)
 *  2. onstop handler registered BEFORE mediaRecorder.stop() every time
 *  3. iOS-safe MIME type detection (mp4 preferred on iOS)
 *  4. Auto-stop timer cleared properly to avoid duplicate stops
 *  5. playAudio never rejects — always resolves so the loop keeps going
 */

// ─────────────────────────────────────────────────────────────────────────────
// Global state
// ─────────────────────────────────────────────────────────────────────────────

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let stream = null;
let callActive = false;
let statusEl = null;
let startBtn = null;
let endBtn = null;
let transcriptList = null;
let emptyState = null;
let autoStopTimer = null;   // FIX: track timer so we can clear it
let currentPlaybackAudio = null;
let recordingVadStopper = null;
let speakingInterruptStopper = null;

// ─────────────────────────────────────────────────────────────────────────────
// Platform detection
// ─────────────────────────────────────────────────────────────────────────────

function isIOS() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function isAndroid() {
  return /Android/.test(navigator.userAgent);
}

function isMobile() {
  return isIOS() || isAndroid();
}

// ─────────────────────────────────────────────────────────────────────────────
// MIME type selection — iOS only supports mp4/aac, NOT webm
// ─────────────────────────────────────────────────────────────────────────────

function getSupportedMimeType() {
  // iOS Safari: only audio/mp4 works reliably
  if (isIOS()) {
    const iosCandidates = ["audio/mp4", "audio/aac", "audio/mpeg"];
    for (const mime of iosCandidates) {
      if (MediaRecorder.isTypeSupported(mime)) {
        console.log("[IST] iOS MIME selected:", mime);
        return mime;
      }
    }
    // iOS 17+ supports mp4 even if isTypeSupported returns false — use it anyway
    console.log("[IST] iOS fallback MIME: audio/mp4");
    return "audio/mp4";
  }

  // Desktop / Android
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
    "audio/wav",
  ];
  for (const mime of candidates) {
    if (MediaRecorder.isTypeSupported(mime)) {
      console.log("[IST] Desktop MIME selected:", mime);
      return mime;
    }
  }
  return "audio/webm";
}

let selectedMimeType = "audio/webm"; // will be set in DOMContentLoaded

// ─────────────────────────────────────────────────────────────────────────────
// Initialization
// ─────────────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  statusEl       = document.getElementById("status");
  startBtn       = document.getElementById("startBtn");
  endBtn         = document.getElementById("endBtn");
  transcriptList = document.getElementById("transcriptList");
  emptyState     = document.getElementById("emptyState");

  selectedMimeType = getSupportedMimeType();
  console.log("[IST] Script loaded | iOS:", isIOS(), "| MIME:", selectedMimeType);
});

// ─────────────────────────────────────────────────────────────────────────────
// iOS audio unlock helper
// On iOS Safari the audio autoplay policy is tied to a synchronous user-gesture
// chain.  Any `await` (network I/O) BEFORE audio.play() severs that chain and
// causes play() to fail silently.  Calling unlockAudio() synchronously at the
// START of the click handler (before the first await) keeps the audio session
// alive for all subsequent plays in this call.
// ─────────────────────────────────────────────────────────────────────────────

function unlockAudio() {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const buf = ctx.createBuffer(1, 1, 22050);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(0);
    ctx.resume().catch(() => {});
  } catch (e) {
    // Non-fatal — ignore if AudioContext not supported
  }
}

function stopPlayback() {
  if (currentPlaybackAudio) {
    try { currentPlaybackAudio.pause(); } catch (_) {}
    try { currentPlaybackAudio.src = ""; } catch (_) {}
    currentPlaybackAudio = null;
  }
  if (speakingInterruptStopper) {
    speakingInterruptStopper();
    speakingInterruptStopper = null;
  }
}

function startLevelMonitor({
  sourceStream,
  sampleMs = 100,
  baselineMs = 400,
  minFloor = 0.012,
  thresholdMultiplier = 2.8,
  speechFramesNeeded = 3,
  silenceMsToTrigger = 1000,
  onSilence,
  onSpeech,
}) {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx || !sourceStream) return () => {};

  const ctx = new AudioCtx();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  analyser.smoothingTimeConstant = 0.2;
  const micSource = ctx.createMediaStreamSource(sourceStream);
  micSource.connect(analyser);

  const data = new Float32Array(analyser.fftSize);
  const baselineLevels = [];
  const startAt = Date.now();
  let lastSpeechAt = Date.now();
  let speechFrames = 0;
  let stopped = false;

  const interval = setInterval(() => {
    if (stopped) return;
    analyser.getFloatTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
    const rms = Math.sqrt(sum / data.length);

    if (Date.now() - startAt < baselineMs) {
      baselineLevels.push(rms);
    }
    const baseline = baselineLevels.length
      ? baselineLevels.reduce((a, b) => a + b, 0) / baselineLevels.length
      : 0;
    const threshold = Math.max(minFloor, baseline * thresholdMultiplier);

    if (rms > threshold) {
      speechFrames += 1;
      lastSpeechAt = Date.now();
      if (speechFrames >= speechFramesNeeded && onSpeech) onSpeech();
    } else {
      speechFrames = 0;
      if ((Date.now() - lastSpeechAt) >= silenceMsToTrigger && onSilence) onSilence();
    }
  }, sampleMs);

  return () => {
    if (stopped) return;
    stopped = true;
    clearInterval(interval);
    try { micSource.disconnect(); } catch (_) {}
    try { analyser.disconnect(); } catch (_) {}
    try { ctx.close(); } catch (_) {}
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Call control
// ─────────────────────────────────────────────────────────────────────────────

async function startCall() {
  if (callActive) return;

  // MUST unlock audio BEFORE any await — iOS Safari loses the user-gesture
  // audio context as soon as the first asynchronous hop (network request) occurs.
  unlockAudio();

  try {
    startBtn.disabled = true;
    updateStatus("Initializing...");

    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        sampleRate: 16000,
      },
    });

    callActive = true;
    startBtn.style.display = "none";
    endBtn.style.display = "inline-flex";
    if (emptyState) emptyState.style.display = "none";

    // Fetch and play greeting
    updateStatus("Loading greeting...");
    try {
      const greetingResp = await fetch("/api/greeting");
      const greetingData = await greetingResp.json();
      // Always show greeting text so the user knows what to say
      if (greetingData.text) {
        addTranscript(greetingData.text, "agent");
      }
    if (greetingData.audio) {
      updateStatus("Speaking... 🔊");
      await playAudio(greetingData.audio);
    }
    } catch (e) {
      console.warn("[IST] Greeting fetch/play error:", e);
      // Non-fatal — continue to listening
    }

    updateStatus("Listening... 🎤");
    startListening();

  } catch (err) {
    console.error("[IST] Start call error:", err);
    let message = "❌ Cannot access microphone.";
    if (err.name === "NotAllowedError")  message = "❌ Microphone permission denied. Check browser settings.";
    if (err.name === "NotFoundError")    message = "❌ No microphone found on this device.";
    updateStatus(message, true);
    startBtn.disabled = false;
    callActive = false;
  }
}

async function endCall() {
  if (!callActive) return;

  callActive = false;
  clearAutoStop();
  stopPlayback();
  if (recordingVadStopper) {
    recordingVadStopper();
    recordingVadStopper = null;
  }

  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }
  if (stream) {
    stream.getTracks().forEach(t => t.stop());
    stream = null;
  }
  mediaRecorder = null;
  isRecording   = false;

  updateStatus("Ending call...");
  try {
    await fetch("/api/call/end", { method: "POST" });
  } catch (_) {}

  startBtn.style.display = "inline-flex";
  endBtn.style.display   = "none";
  startBtn.disabled      = false;
  updateStatus("Call ended. Click Start to begin again.");

  transcriptList.innerHTML = '<div class="empty-state">No conversation yet. Start a call and speak.</div>';
  emptyState = transcriptList.querySelector(".empty-state");
}

// ─────────────────────────────────────────────────────────────────────────────
// Recording — FIX: attach onstop BEFORE calling stop()
// ─────────────────────────────────────────────────────────────────────────────

function clearAutoStop() {
  if (autoStopTimer) {
    clearTimeout(autoStopTimer);
    autoStopTimer = null;
  }
}

function startListening() {
  if (!callActive) return;
  if (isRecording)  return;

  // Create a fresh MediaRecorder for each turn (most reliable on iOS)
  try {
    const options = {};
    // Only pass mimeType if it's actually supported (some iOS versions reject unknown types)
    if (MediaRecorder.isTypeSupported(selectedMimeType)) {
      options.mimeType = selectedMimeType;
    }
    mediaRecorder = new MediaRecorder(stream, options);
  } catch (e) {
    console.warn("[IST] MediaRecorder creation failed, using defaults:", e);
    mediaRecorder = new MediaRecorder(stream);
  }

  audioChunks = [];
  isRecording  = true;
  if (recordingVadStopper) {
    recordingVadStopper();
    recordingVadStopper = null;
  }

  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) {
      audioChunks.push(e.data);
    }
  };

  // FIX: attach onstop BEFORE start() so it's ready when stop fires
  mediaRecorder.onstop = async () => {
    isRecording = false;
    clearAutoStop();
    if (recordingVadStopper) {
      recordingVadStopper();
      recordingVadStopper = null;
    }

    if (!callActive) return;   // call was ended during recording

    try {
      const mimeUsed = mediaRecorder.mimeType || selectedMimeType || "audio/mp4";
      const audioBlob = new Blob(audioChunks, { type: mimeUsed });
      console.log("[IST] Recorded blob:", audioBlob.size, "bytes, type:", mimeUsed);

      if (audioBlob.size < 200) {
        console.warn("[IST] Audio too short, re-listening");
        updateStatus("Listening... 🎤");
        startListening();
        return;
      }

      updateStatus("Processing... ⏳");
      await sendAudioToServer(audioBlob, mimeUsed);
    } catch (err) {
      console.error("[IST] onstop error:", err);
      if (callActive) {
        updateStatus("Listening... 🎤");
        startListening();
      }
    }
  };

  mediaRecorder.onerror = (e) => {
    console.error("[IST] MediaRecorder error:", e.error || e);
    isRecording = false;
    clearAutoStop();
    if (recordingVadStopper) {
      recordingVadStopper();
      recordingVadStopper = null;
    }
    if (callActive) {
      updateStatus("Listening... 🎤");
      startListening();
    }
  };

  mediaRecorder.start(250);   // small chunks improve recorder data flushing, especially on iOS Safari
  console.log("[IST] Recording started, state:", mediaRecorder.state);

  // VAD: once user speech has started, stop recording after 1s silence
  let speechDetected = false;
  recordingVadStopper = startLevelMonitor({
    sourceStream: stream,
    sampleMs: 100,
    baselineMs: 500,
    minFloor: 0.014,
    thresholdMultiplier: 3.0,
    speechFramesNeeded: 3,
    silenceMsToTrigger: 1000,
    onSpeech: () => { speechDetected = true; },
    onSilence: () => {
      if (!speechDetected) return; // ignore pure background/noise before real speech
      if (isRecording && mediaRecorder && mediaRecorder.state === "recording") {
        console.log("[IST] VAD silence detected >1s, stopping recording");
        mediaRecorder.stop();
      }
    },
  });

  // Auto-stop after 15 seconds
  clearAutoStop();
  autoStopTimer = setTimeout(() => {
    if (isRecording && mediaRecorder && mediaRecorder.state === "recording") {
      console.log("[IST] Auto-stop triggered");
      mediaRecorder.stop();
    }
  }, 15000);
}

// ─────────────────────────────────────────────────────────────────────────────
// Server communication
// ─────────────────────────────────────────────────────────────────────────────

function getExtensionForMime(mime) {
  if (!mime) return "mp4";
  if (mime.includes("webm")) return "webm";
  if (mime.includes("mp4") || mime.includes("m4a") || mime.includes("aac")) return "m4a";
  if (mime.includes("wav"))  return "wav";
  if (mime.includes("ogg"))  return "ogg";
  return "mp4";
}

async function sendAudioToServer(audioBlob, mimeUsed) {
  try {
    const ext      = getExtensionForMime(mimeUsed);
    const filename = `audio.${ext}`;
    const formData = new FormData();
    formData.append("audio", audioBlob, filename);

    console.log("[IST] Sending audio:", filename, audioBlob.size, "bytes");

    const response = await fetch("/api/call/process", {
      method: "POST",
      body:   formData,
      // Do NOT set Content-Type — browser sets it with boundary for multipart
    });

    if (!response.ok) {
      throw new Error(`Server error: ${response.status}`);
    }

    const data = await response.json();
    console.log("[IST] Server response:", data);

    if (data.transcript) addTranscript(data.transcript, "you");
    if (data.reply)      addTranscript(data.reply,      "agent");

    if (data.audio) {
      updateStatus("Speaking... 🔊");
      await playAudio(data.audio);
    }

    if (data.end_call) {
      callActive = false;
      startBtn.style.display = "inline-flex";
      endBtn.style.display   = "none";
      startBtn.disabled      = false;
      updateStatus("Call ended. Thank you!");
      if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
      return;
    }

    if (callActive) {
      updateStatus("Listening... 🎤");
      startListening();
    }

  } catch (err) {
    console.error("[IST] Server error:", err);
    updateStatus("❌ Connection error. Retrying...");
    await sleep(1500);
    if (callActive) {
      updateStatus("Listening... 🎤");
      startListening();
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Audio playback — FIX: removed crossOrigin (breaks iOS), always resolves
// ─────────────────────────────────────────────────────────────────────────────

function playAudio(audioUrl) {
  return new Promise((resolve) => {
    stopPlayback();
    const audio = new Audio();
    currentPlaybackAudio = audio;

    // FIX: do NOT set crossOrigin on iOS — it triggers CORS preflight that
    // fails for same-origin /static/ files on Safari, breaking playback.
    // audio.crossOrigin = "anonymous";  ← REMOVED

    audio.volume   = 1.0;
    audio.preload  = "auto";
    audio.setAttribute("playsinline", "true");
    audio.setAttribute("webkit-playsinline", "true");

    let settled = false;
    const done = () => {
      if (!settled) {
        settled = true;
        clearTimeout(safetyTimer);
        if (currentPlaybackAudio === audio) {
          currentPlaybackAudio = null;
        }
        if (speakingInterruptStopper) {
          speakingInterruptStopper();
          speakingInterruptStopper = null;
        }
        resolve();
      }
    };

    // Safety timeout — always resolve so the conversation loop continues
    const safetyTimer = setTimeout(() => {
      console.warn("[IST] Audio playback timeout — continuing anyway");
      audio.pause();
      done();
    }, 45000);

    audio.onended  = () => { console.log("[IST] Audio ended"); done(); };
    audio.onerror  = (e) => { console.error("[IST] Audio error:", e); done(); };
    audio.onpause  = () => {
      // If paused by barge-in/end-call, continue conversation loop safely.
      if (!audio.ended) done();
    };

    audio.src = audioUrl;

    // During speaking, allow user interruption (barge-in) but require
    // sustained speech to avoid triggering on brief background noise.
    if (callActive && stream) {
      speakingInterruptStopper = startLevelMonitor({
        sourceStream: stream,
        sampleMs: 100,
        baselineMs: 500,
        minFloor: 0.016,
        thresholdMultiplier: 3.4,
        speechFramesNeeded: 4,
        onSpeech: () => {
          if (!callActive || !currentPlaybackAudio) return;
          console.log("[IST] User interruption detected; stopping TTS");
          stopPlayback();
          if (!isRecording) {
            updateStatus("Listening... 🎤");
            startListening();
          }
        },
      });
    }

    // iOS requires play() to be called directly from a user-gesture chain.
    // We're already inside a user-initiated flow so this should work.
    const p = audio.play();
    if (p && typeof p.then === "function") {
      p.then(() => console.log("[IST] Playback started"))
       .catch((err) => {
          console.error("[IST] play() rejected:", err);
          // Still resolve so the loop continues
          done();
        });
    }
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// UI helpers
// ─────────────────────────────────────────────────────────────────────────────

function updateStatus(message, isError = false) {
  if (!statusEl) return;
  statusEl.textContent = message;
  statusEl.className   = "status";
  statusEl.removeAttribute("style");

  if (isError) {
    statusEl.style.background   = "rgba(239, 68, 68, 0.12)";
    statusEl.style.borderColor  = "#ef4444";
    statusEl.style.color        = "#ef4444";
  } else if (message.includes("🎤")) {
    statusEl.classList.add("listening");
  } else if (message.includes("⏳")) {
    statusEl.classList.add("processing");
  } else if (message.includes("🔊")) {
    statusEl.classList.add("speaking");
  }
}

function addTranscript(text, role) {
  if (!transcriptList) return;

  const existing = transcriptList.querySelector(".empty-state");
  if (existing) existing.remove();

  const entry = document.createElement("div");
  entry.className = `entry ${role === "you" ? "you" : "agent"}`;

  const roleDiv    = document.createElement("div");
  roleDiv.className = "role";
  roleDiv.textContent = role === "you" ? "You" : "IST Assistant";

  const contentDiv = document.createElement("div");
  contentDiv.className   = "content";
  contentDiv.textContent = text;

  entry.appendChild(roleDiv);
  entry.appendChild(contentDiv);
  transcriptList.appendChild(entry);
  transcriptList.scrollTop = transcriptList.scrollHeight;
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// ─────────────────────────────────────────────────────────────────────────────
// Cleanup
// ─────────────────────────────────────────────────────────────────────────────

window.addEventListener("beforeunload", () => {
  stopPlayback();
  if (stream)       stream.getTracks().forEach(t => t.stop());
  if (isRecording && mediaRecorder) mediaRecorder.stop();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden && isRecording && mediaRecorder) {
    mediaRecorder.stop();
  }
});
