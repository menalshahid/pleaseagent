/**
 * IST Voice Assistant - PRODUCTION HARDENED
 * ✓ Android + iOS + Web stable
 * ✓ Proper VAD with per-platform tuning
 * ✓ Reduced latency (parallel TTS generation)
 * ✓ Aggressive retry logic + fallbacks
 */

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
let autoStopTimer = null;
let currentPlaybackAudio = null;
let recordingVadStopper = null;
let speakingInterruptStopper = null;
let callId = null;
let retryCount = 0;
const MAX_RETRIES = 3;

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

function getPlatform() {
  if (isIOS()) return "ios";
  if (isAndroid()) return "android";
  return "web";
}

function newCallId() {
  try {
    if (crypto && crypto.randomUUID) return crypto.randomUUID();
    if (crypto && crypto.getRandomValues) {
      const b = new Uint8Array(16);
      crypto.getRandomValues(b);
      b[6] = (b[6] & 0x0f) | 0x40;
      b[8] = (b[8] & 0x3f) | 0x80;
      const hex = Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
    }
  } catch (_) {}
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// MIME type selection - PRODUCTION HARDENED
// ─────────────────────────────────────────────────────────────────────────────

function getSupportedMimeType() {
  const platform = getPlatform();
  
  if (platform === "ios") {
    // iOS: audio/mp4 is safest, fallback to AAC
    const candidates = ["audio/mp4", "audio/aac"];
    for (const mime of candidates) {
      if (MediaRecorder.isTypeSupported(mime)) {
        console.log(`[IST] iOS MIME: ${mime}`);
        return mime;
      }
    }
    console.log("[IST] iOS fallback: audio/mp4 (force)");
    return "audio/mp4";
  }

  if (platform === "android") {
    // Android: webm > ogg > mp4
    const candidates = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/mp4",
    ];
    for (const mime of candidates) {
      if (MediaRecorder.isTypeSupported(mime)) {
        console.log(`[IST] Android MIME: ${mime}`);
        return mime;
      }
    }
    return "audio/webm";
  }

  // Desktop/Web
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
  ];
  for (const mime of candidates) {
    if (MediaRecorder.isTypeSupported(mime)) {
      console.log(`[IST] Web MIME: ${mime}`);
      return mime;
    }
  }
  return "audio/webm";
}

let selectedMimeType = "audio/webm";

// ─────────────────────────────────────────────────────────────────────────────
// Initialization
// ─────────────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  statusEl = document.getElementById("status");
  startBtn = document.getElementById("startBtn");
  endBtn = document.getElementById("endBtn");
  transcriptList = document.getElementById("transcriptList");
  emptyState = document.getElementById("emptyState");

  selectedMimeType = getSupportedMimeType();
  callId = newCallId();
  console.log(`[IST] Init | Platform: ${getPlatform()} | MIME: ${selectedMimeType}`);
});

// ─────────────────────────────────────────────────────────────────────────────
// iOS audio unlock
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
    console.warn("[IST] Audio unlock failed (non-fatal):", e.message);
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

// ─────────────────────────────────────────────────────────────────────────────
// VAD with platform-specific tuning
// ─────────────────────────────────────────────────────────────────────────────

function getVADParams() {
  const platform = getPlatform();
  
  // Default params - conservative across all platforms
  const base = {
    sampleMs: 100,
    baselineMs: 600,
    minFloor: 0.015,
    thresholdMultiplier: 2.5,
    speechFramesNeeded: 4,
    silenceMsToTrigger: 1200,
  };

  if (platform === "ios") {
    return {
      ...base,
      minFloor: 0.018,
      thresholdMultiplier: 2.8,
      speechFramesNeeded: 3,
    };
  }

  if (platform === "android") {
    return {
      ...base,
      minFloor: 0.016,
      thresholdMultiplier: 2.4,
      speechFramesNeeded: 5,
      silenceMsToTrigger: 1500, // Android needs longer silence
    };
  }

  // Web desktop
  return base;
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

  try {
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
      try {
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
      } catch (e) {
        console.warn("[IST] VAD analysis error (non-fatal):", e.message);
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
  } catch (e) {
    console.warn("[IST] VAD initialization failed:", e.message);
    return () => {};
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Call control - HARDENED
// ─────────────────────────────────────────────────────────────────────────────

async function startCall() {
  if (callActive) return;

  unlockAudio();

  try {
    if (!callId) callId = newCallId();
    retryCount = 0;
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

    // Fetch greeting (non-blocking - show text immediately)
    updateStatus("Loading greeting...");
    try {
      const greetingResp = await Promise.race([
        fetch(`/api/greeting?call_id=${encodeURIComponent(callId)}`),
        new Promise((_, reject) => setTimeout(() => reject(new Error("Timeout")), 5000))
      ]);
      const greetingData = await greetingResp.json();
      
      if (greetingData.text) {
        addTranscript(greetingData.text, "agent");
      }

      // Play audio asynchronously (don't block on it)
      if (greetingData.audio) {
        updateStatus("Speaking... 🔊");
        playAudio(greetingData.audio).catch(() => console.warn("[IST] Greeting play failed (non-blocking)"));
      }
    } catch (e) {
      console.warn("[IST] Greeting error (non-fatal):", e.message);
      updateStatus("Listening... 🎤");
    }

    if (callActive) {
      updateStatus("Listening... 🎤");
      startListening();
    }

  } catch (err) {
    console.error("[IST] Start call error:", err);
    let message = "❌ Cannot access microphone.";
    if (err.name === "NotAllowedError") message = "❌ Permission denied. Enable microphone in settings.";
    if (err.name === "NotFoundError") message = "❌ No microphone found.";
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
    try { mediaRecorder.stop(); } catch (_) {}
  }
  
  if (stream) {
    stream.getTracks().forEach(t => {
      try { t.stop(); } catch (_) {}
    });
    stream = null;
  }
  
  mediaRecorder = null;
  isRecording = false;

  updateStatus("Ending call...");
  
  try {
    await Promise.race([
      fetch("/api/call/end", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ call_id: callId }),
      }),
      new Promise((_, reject) => setTimeout(() => reject(new Error("Timeout")), 3000))
    ]);
  } catch (_) {}

  callId = newCallId();
  startBtn.style.display = "inline-flex";
  endBtn.style.display = "none";
  startBtn.disabled = false;
  updateStatus("Call ended. Click Start to begin again.");

  transcriptList.innerHTML = '<div class="empty-state">No conversation yet. Start a call and speak.</div>';
  emptyState = transcriptList.querySelector(".empty-state");
}

// ─────────────────────────────────────────────────────────────────────────────
// Recording with aggressive retry
// ─────────────────────────────────────────────────────────────────────────────

function clearAutoStop() {
  if (autoStopTimer) {
    clearTimeout(autoStopTimer);
    autoStopTimer = null;
  }
}

function startListening() {
  if (!callActive) return;
  if (isRecording) return;

  try {
    const options = {};
    if (MediaRecorder.isTypeSupported(selectedMimeType)) {
      options.mimeType = selectedMimeType;
    }
    mediaRecorder = new MediaRecorder(stream, options);
  } catch (e) {
    console.warn("[IST] MediaRecorder creation error:", e);
    mediaRecorder = new MediaRecorder(stream);
  }

  audioChunks = [];
  isRecording = true;

  if (recordingVadStopper) {
    recordingVadStopper();
    recordingVadStopper = null;
  }

  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) {
      audioChunks.push(e.data);
    }
  };

  // FIX: Attach BEFORE start()
  mediaRecorder.onstop = async () => {
    isRecording = false;
    clearAutoStop();
    
    if (recordingVadStopper) {
      recordingVadStopper();
      recordingVadStopper = null;
    }

    if (!callActive) return;

    try {
      const mimeUsed = mediaRecorder.mimeType || selectedMimeType || "audio/mp4";
      const audioBlob = new Blob(audioChunks, { type: mimeUsed });
      
      console.log(`[IST] Recorded: ${audioBlob.size} bytes (${mimeUsed})`);

      if (audioBlob.size < 200) {
        console.warn("[IST] Audio too short, re-listening");
        if (callActive) {
          updateStatus("Listening... 🎤");
          startListening();
        }
        return;
      }

      retryCount = 0; // Reset retry on successful recording
      updateStatus("Processing... ⏳");
      await sendAudioToServer(audioBlob, mimeUsed);

    } catch (err) {
      console.error("[IST] Recording onstop error:", err);
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

  try {
    mediaRecorder.start(250);
  } catch (e) {
    console.error("[IST] mediaRecorder.start() failed:", e);
    isRecording = false;
    if (callActive) {
      updateStatus("Listening... 🎤");
      setTimeout(() => startListening(), 500);
    }
    return;
  }

  // VAD - tuned per platform
  const vadParams = getVADParams();
  let speechDetected = false;
  
  recordingVadStopper = startLevelMonitor({
    sourceStream: stream,
    ...vadParams,
    onSpeech: () => { speechDetected = true; },
    onSilence: () => {
      if (!speechDetected) return;
      if (isRecording && mediaRecorder && mediaRecorder.state === "recording") {
        console.log("[IST] VAD silence detected, stopping");
        try { mediaRecorder.stop(); } catch (_) {}
      }
    },
  });

  // Auto-stop at 15 seconds
  clearAutoStop();
  autoStopTimer = setTimeout(() => {
    if (isRecording && mediaRecorder && mediaRecorder.state === "recording") {
      console.log("[IST] Auto-stop at 15s");
      try { mediaRecorder.stop(); } catch (_) {}
    }
  }, 15000);
}

// ─────────────────────────────────────────────────────────────────────────────
// Server communication with RETRY LOGIC
// ─────────────────────────────────────────────────────────────────────────────

function getExtensionForMime(mime) {
  if (!mime) return "m4a";
  if (mime.includes("webm")) return "webm";
  if (mime.includes("mp4") || mime.includes("m4a") || mime.includes("aac")) return "m4a";
  if (mime.includes("wav")) return "wav";
  if (mime.includes("ogg")) return "ogg";
  return "m4a";
}

async function sendAudioToServer(audioBlob, mimeUsed) {
  const maxRetries = 3;
  let lastError = null;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const ext = getExtensionForMime(mimeUsed);
      const filename = `audio.${ext}`;
      const formData = new FormData();
      formData.append("audio", audioBlob, filename);
      formData.append("call_id", callId);

      console.log(`[IST] Sending audio (attempt ${attempt + 1}/${maxRetries + 1}): ${filename}`);

      const response = await Promise.race([
        fetch("/api/call/process", {
          method: "POST",
          body: formData,
        }),
        new Promise((_, reject) => setTimeout(() => reject(new Error("Request timeout")), 30000))
      ]);

      if (!response.ok) {
        throw new Error(`Server error: ${response.status}`);
      }

      const data = await response.json();
      console.log("[IST] Server response received");

      // Reset retry counter on success
      retryCount = 0;

      if (data.transcript) addTranscript(data.transcript, "you");
      if (data.reply) addTranscript(data.reply, "agent");

      if (data.audio) {
        updateStatus("Speaking... 🔊");
        // Play async - don't block
        playAudio(data.audio).catch(e => console.warn("[IST] Playback error:", e));
      }

      if (data.end_call) {
        callActive = false;
        startBtn.style.display = "inline-flex";
        endBtn.style.display = "none";
        startBtn.disabled = false;
        updateStatus("Call ended. Thank you!");
        if (stream) {
          stream.getTracks().forEach(t => {
            try { t.stop(); } catch (_) {}
          });
          stream = null;
        }
        return;
      }

      if (callActive) {
        updateStatus("Listening... 🎤");
        startListening();
      }
      return; // Success - exit retry loop

    } catch (err) {
      lastError = err;
      console.error(`[IST] Attempt ${attempt + 1} failed:`, err.message);

      if (attempt < maxRetries) {
        const backoff = Math.min(1000 * Math.pow(2, attempt), 5000);
        console.log(`[IST] Retrying in ${backoff}ms...`);
        await sleep(backoff);
      }
    }
  }

  // All retries exhausted
  console.error(`[IST] All ${maxRetries + 1} attempts failed:`, lastError);
  if (callActive) {
    updateStatus("❌ Server error. Retrying...");
    await sleep(2000);
    if (callActive) {
      updateStatus("Listening... 🎤");
      startListening();
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Audio playback - HARDENED
// ─────────────────────────────────────────────────────────────────────────────

function playAudio(audioUrl) {
  return new Promise((resolve) => {
    stopPlayback();
    const audio = new Audio();
    currentPlaybackAudio = audio;

    audio.volume = 1.0;
    audio.preload = "auto";
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

    // 60 second timeout (safety)
    const safetyTimer = setTimeout(() => {
      console.warn("[IST] Playback timeout - continuing");
      try { audio.pause(); } catch (_) {}
      done();
    }, 60000);

    audio.onended = () => { console.log("[IST] Audio ended"); done(); };
    audio.onerror = (e) => { console.error("[IST] Audio error:", e); done(); };
    audio.onpause = () => {
      if (!audio.ended) done();
    };

    audio.src = audioUrl;

    // Barge-in detection (user interruption)
    if (callActive && stream) {
      const bargeInParams = getVADParams();
      bargeInParams.minFloor = 0.020; // Higher threshold for barge-in
      bargeInParams.thresholdMultiplier = 3.2;
      bargeInParams.speechFramesNeeded = 5; // Require sustained speech

      speakingInterruptStopper = startLevelMonitor({
        sourceStream: stream,
        ...bargeInParams,
        onSpeech: () => {
          if (!callActive || !currentPlaybackAudio) return;
          console.log("[IST] Barge-in detected");
          stopPlayback();
          if (!isRecording) {
            updateStatus("Listening... 🎤");
            startListening();
          }
        },
      });
    }

    // Play with error handling
    const p = audio.play();
    if (p && typeof p.then === "function") {
      p.then(() => console.log("[IST] Playback started"))
        .catch((err) => {
          console.error("[IST] Play failed:", err.message);
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
  statusEl.className = "status";
  statusEl.removeAttribute("style");

  if (isError) {
    statusEl.style.background = "rgba(239, 68, 68, 0.12)";
    statusEl.style.borderColor = "#ef4444";
    statusEl.style.color = "#ef4444";
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

  const roleDiv = document.createElement("div");
  roleDiv.className = "role";
  roleDiv.textContent = role === "you" ? "You" : "IST Assistant";

  const contentDiv = document.createElement("div");
  contentDiv.className = "content";
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
  if (stream) stream.getTracks().forEach(t => {
    try { t.stop(); } catch (_) {}
  });
  if (isRecording && mediaRecorder) {
    try { mediaRecorder.stop(); } catch (_) {}
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden && isRecording && mediaRecorder) {
    try { mediaRecorder.stop(); } catch (_) {}
  }
});