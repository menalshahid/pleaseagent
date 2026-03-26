/**
 * IST Voice Assistant - Mobile-optimized audio recording & playback
 * Supports iOS, Android, and desktop browsers
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

// Audio format preference: try to use webm on desktop, but fall back gracefully
const AUDIO_MIMETYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/wav",
  "audio/ogg"
];

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

  // Detect supported mime type
  for (const mime of AUDIO_MIMETYPES) {
    if (MediaRecorder.isTypeSupported(mime)) {
      selectedMimeType = mime;
      break;
    }
  }

  console.log("[IST] Using audio MIME type:", selectedMimeType);

  // Request permissions on load (iOS requirement)
  if (isIOS()) {
    requestAudioPermissionOnce();
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Platform detection
// ─────────────────────────────────────────────────────────────────────────────

function isIOS() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent);
}

function isAndroid() {
  return /Android/.test(navigator.userAgent);
}

function isMobile() {
  return isIOS() || isAndroid();
}

// ─────────────────────────────────────────────────────────────────────────────
// Audio permission handling
// ─────────────────────────────────────────────────────────────────────────────

let permissionRequested = false;

async function requestAudioPermissionOnce() {
  if (permissionRequested) return;
  permissionRequested = true;

  try {
    const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    tempStream.getTracks().forEach(t => t.stop());
    console.log("[IST] Audio permission granted");
  } catch (err) {
    console.warn("[IST] Audio permission denied:", err.message);
    updateStatus("⚠️ Microphone permission needed. Please enable in settings.", true);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Call control (start/end)
// ─────────────────────────────────────────────────────────────────────────────

async function startCall() {
  if (callActive) return;

  try {
    startBtn.disabled = true;
    updateStatus("Initializing...");

    // Request audio permission
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    // Initialize MediaRecorder
    const options = { mimeType: selectedMimeType };
    mediaRecorder = new MediaRecorder(stream, options);
    audioChunks = [];

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) {
        audioChunks.push(e.data);
      }
    };

    mediaRecorder.onerror = (e) => {
      console.error("[IST] MediaRecorder error:", e.error);
      updateStatus("❌ Recording error. Please try again.", true);
      stopRecording();
    };

    callActive = true;
    startBtn.style.display = "none";
    endBtn.style.display = "inline-flex";
    emptyState.style.display = "none";

    // Fetch greeting and play
    updateStatus("Loading greeting...");
    const greetingResp = await fetch("/api/greeting");
    const greetingData = await greetingResp.json();

    if (greetingData.audio) {
      await playAudio(greetingData.audio);
    }

    updateStatus("Listening... 🎤");
    startListening();
  } catch (err) {
    console.error("[IST] Start call error:", err);
    let message = "❌ Cannot access microphone.";
    if (err.name === "NotAllowedError") {
      message = "❌ Microphone permission denied. Check browser settings.";
    } else if (err.name === "NotFoundError") {
      message = "❌ No microphone found on this device.";
    }
    updateStatus(message, true);
    startBtn.disabled = false;
  }
}

async function endCall() {
  if (!callActive) return;

  try {
    stopRecording();
    callActive = false;
    updateStatus("Ending call...");

    await fetch("/api/call/end", { method: "POST" });

    startBtn.style.display = "inline-flex";
    endBtn.style.display = "none";
    startBtn.disabled = false;
    updateStatus("Call ended. Click Start to begin again.", false);

    // Clear transcript
    transcriptList.innerHTML = '<div class="empty-state">No conversation yet. Start a call and speak.</div>';
    emptyState = transcriptList.querySelector(".empty-state");
  } catch (err) {
    console.error("[IST] End call error:", err);
    updateStatus("⚠️ Error ending call. Please refresh.", true);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Recording control
// ─────────────────────────────────────────────────────────────────────────────

function startListening() {
  if (isRecording || !mediaRecorder) return;

  audioChunks = [];
  isRecording = true;

  // Auto-stop after 15 seconds of silence or 30 seconds max
  mediaRecorder.start();
  setTimeout(stopRecording, 30000); // 30 sec max
}

function stopRecording() {
  if (!isRecording || !mediaRecorder) return;

  isRecording = false;
  mediaRecorder.stop();

  // Process audio when recording stops
  mediaRecorder.onstop = async () => {
    try {
      const audioBlob = new Blob(audioChunks, { type: selectedMimeType });
      if (audioBlob.size < 100) {
        updateStatus("Audio too short. Please try again.");
        startListening();
        return;
      }

      updateStatus("Processing... ⏳");
      await sendAudioToServer(audioBlob);
    } catch (err) {
      console.error("[IST] Audio processing error:", err);
      updateStatus("❌ Error processing audio. Try again.");
      if (callActive) {
        startListening();
      }
    }
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Server communication
// ─────────────────────────────────────────────────────────────────────────────

async function sendAudioToServer(audioBlob) {
  try {
    const formData = new FormData();
    formData.append("audio", audioBlob, `audio.${getAudioExtension()}`);

    const response = await fetch("/api/call/process", {
      method: "POST",
      body: formData,
      headers: {
        "Accept": "application/json",
      },
    });

    if (!response.ok) {
      throw new Error(`Server error: ${response.status}`);
    }

    const data = await response.json();

    // Update transcript
    if (data.transcript) {
      addTranscript(data.transcript, "you");
    }
    if (data.reply) {
      addTranscript(data.reply, "agent");
    }

    // Play audio response
    if (data.audio) {
      updateStatus("Playing response... 🔊");
      await playAudio(data.audio);
    }

    // Check if call should end
    if (data.end_call) {
      callActive = false;
      startBtn.style.display = "inline-flex";
      endBtn.style.display = "none";
      startBtn.disabled = false;
      updateStatus("Call ended. Thank you!", false);
      if (stream) {
        stream.getTracks().forEach(t => t.stop());
      }
      return;
    }

    // Resume listening
    if (callActive) {
      updateStatus("Listening... 🎤");
      startListening();
    }
  } catch (err) {
    console.error("[IST] Server error:", err);
    updateStatus("❌ Connection error. Please try again.");
    if (callActive) {
      startListening();
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Audio playback
// ─────────────────────────────────────────────────────────────────────────────

function playAudio(audioUrl) {
  return new Promise((resolve, reject) => {
    const audio = new Audio(audioUrl);

    // Set up event handlers
    audio.onended = () => {
      console.log("[IST] Audio playback finished");
      resolve();
    };

    audio.onerror = (err) => {
      console.error("[IST] Audio playback error:", err);
      reject(err);
    };

    // Critical for mobile: must set playback properties BEFORE play()
    audio.volume = 1.0;
    audio.crossOrigin = "anonymous";

    // Play with error handling
    const playPromise = audio.play();
    if (playPromise !== undefined) {
      playPromise
        .then(() => console.log("[IST] Audio playback started"))
        .catch((err) => {
          console.error("[IST] Play failed:", err);
          reject(err);
        });
    }

    // Safety timeout: resolve after 60 seconds if audio never ends
    const timeout = setTimeout(() => {
      audio.pause();
      resolve();
    }, 60000);

    const cleanupTimeout = () => clearTimeout(timeout);
    audio.onended = () => {
      cleanupTimeout();
      resolve();
    };
    audio.onerror = (err) => {
      cleanupTimeout();
      reject(err);
    };
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// UI helpers
// ─────────────────────────────────────────────────────────────────────────────

function updateStatus(message, isError = false) {
  if (!statusEl) return;
  statusEl.textContent = message;
  statusEl.className = "status";
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

  // Remove empty state on first entry
  if (emptyState && emptyState.parentElement) {
    emptyState.remove();
  }

  const entry = document.createElement("div");
  entry.className = `entry ${role === "you" ? "you" : "agent"}`;

  const roleDiv = document.createElement("div");
  roleDiv.className = "role";
  roleDiv.textContent = role === "you" ? "You" : "IST Assistant";

  const contentDiv = document.createElement("div");
  contentDiv.className = "content";
  contentDiv.textContent = text;
  contentDiv.style.wordWrap = "break-word";
  contentDiv.style.wordBreak = "break-word";

  entry.appendChild(roleDiv);
  entry.appendChild(contentDiv);
  transcriptList.appendChild(entry);

  // Auto-scroll to bottom
  transcriptList.scrollTop = transcriptList.scrollHeight;
}

// ─────────────────────────────────────────────────────────────────────────────
// Utility
// ─────────────────────────────────────────────────────────────────────────────

function getAudioExtension() {
  if (selectedMimeType.includes("webm")) return "webm";
  if (selectedMimeType.includes("mp4")) return "m4a";
  if (selectedMimeType.includes("wav")) return "wav";
  if (selectedMimeType.includes("ogg")) return "ogg";
  return "webm";
}

// ─────────────────────────────────────────────────────────────────────────────
// Global error handling
// ─────────────────────────────────────────────────────────────────────────────

window.addEventListener("beforeunload", () => {
  if (stream) {
    stream.getTracks().forEach(t => t.stop());
  }
  if (isRecording && mediaRecorder) {
    mediaRecorder.stop();
  }
});

// Handle visibility change (pause recording when tab hidden)
document.addEventListener("visibilitychange", () => {
  if (document.hidden && isRecording) {
    stopRecording();
  }
});

console.log("[IST] Script loaded - mobile compatibility:", isMobile());