(function () {
    const SPEECH_THRESHOLD = 18;
    const INTERRUPT_THRESHOLD = 55;
    const INTERRUPT_HITS = 6;
    const SILENCE_MS = 600;
    const MIN_SPEECH_MS = 300;
    const MAX_RECORD_MS = 15000;
    const POLL_MS = 80;
  
    let stream = null;
    let audioContext = null;
    let analyser = null;
    let recorder = null;
    let polling = null;
    let isInCall = false;
    let isProcessing = false;
    let dataArray = null;
    let currentAudio = null;
    let interruptCheck = null;
  
    const statusEl = document.getElementById("status");
    const startBtn = document.getElementById("startBtn");
    const endBtn = document.getElementById("endBtn");
    const transcriptList = document.getElementById("transcriptList");
    const emptyState = document.getElementById("emptyState");
  
    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = "status" + (cls ? " " + cls : "");
    }
  
    function escapeHtml(str) {
      const d = document.createElement("div");
      d.textContent = str;
      return d.innerHTML;
    }
  
    function addEntry(who, content) {
      if (!content || !String(content).trim()) return;
      emptyState.style.display = "none";
      const div = document.createElement("div");
      div.className = "entry " + who;
      const role = who === "you" ? "You said" : "IST Agent";
      div.innerHTML = '<span class="role">' + role + '</span><div class="content">' + escapeHtml(content) + '</div>';
      transcriptList.appendChild(div);
      transcriptList.scrollTop = transcriptList.scrollHeight;
    }
  
    function getVolume() {
      if (!analyser || !dataArray) return 0;
      analyser.getByteFrequencyData(dataArray);
      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) sum += dataArray[i];
      return sum / dataArray.length;
    }
  
    function stopCurrentAudio() {
      if (currentAudio) {
        currentAudio.pause();
        currentAudio.currentTime = 0;
        currentAudio = null;
      }
      if (interruptCheck) {
        clearInterval(interruptCheck);
        interruptCheck = null;
      }
    }
  
    function getAudioFilename(blob) {
      const t = (blob && blob.type) ? blob.type.toLowerCase() : "";
      if (t.includes("webm")) return "audio.webm";
      if (t.includes("mp4") || t.includes("m4a")) return "audio.m4a";
      if (t.includes("ogg")) return "audio.ogg";
      return "audio.webm";
    }

    async function processAudio(blob, filename) {
      if (!blob || blob.size < 500) {
        isProcessing = false;
        return;
      }
      setStatus("Processing…", "processing");
  
      let data = {};
      try {
        const form = new FormData();
        form.append("audio", blob, filename || getAudioFilename(blob));
        const res = await fetch("/api/call/process", { method: "POST", body: form });
        if (!res.ok) {
          addEntry("agent", "Server error. Please try again.");
          setStatus("Listening…", "listening");
          isProcessing = false;
          return;
        }
        data = await res.json();
  
        if (data.transcript && !data.transcript.toLowerCase().includes("sorry")) {
          addEntry("you", data.transcript);
        }
        if (data.reply) {
          addEntry("agent", data.reply);
          if (data.audio) {
            setStatus("Speaking…", "speaking");
            const audio = new Audio(data.audio);
            currentAudio = audio;
            let interruptCount = 0;
            interruptCheck = setInterval(() => {
              if (!isInCall) return;
              const vol = getVolume();
              if (vol > INTERRUPT_THRESHOLD) {
                interruptCount++;
                if (interruptCount >= INTERRUPT_HITS) {
                  stopCurrentAudio();
                  isProcessing = false;
                  setStatus("Listening…", "listening");
                  startRecordingLoop();
                }
              } else {
                interruptCount = 0;
              }
            }, 120);
  
            await new Promise((resolve) => {
              audio.onended = () => {
                stopCurrentAudio();
                resolve();
              };
              audio.onerror = () => {
                stopCurrentAudio();
                resolve();
              };
              audio.play().catch(() => resolve());
            });
  
            if (interruptCheck) {
              clearInterval(interruptCheck);
              interruptCheck = null;
            }
          }
          if (data.end_call) {
            isInCall = false;
            setTimeout(endCall, 500);
            return;
          }
        } else if (!data.reply) {
          addEntry("agent", "Could not understand. Please try again.");
        }
      } catch (e) {
        console.error(e);
        addEntry("agent", "Something went wrong. Please try again.");
      }
      if (isInCall && !data.end_call) {
        setStatus("Listening…", "listening");
      }
      isProcessing = false;
    }
  
    function startRecordingLoop() {
      let chunks = [];
      let speechStartTime = 0;
      let silenceStartTime = 0;
      let state = "waiting";

      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus"
        : MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm"
        : MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4"
        : MediaRecorder.isTypeSupported("audio/mpeg") ? "audio/mpeg" : "";
      const mimeOpt = mime ? { mimeType: mime } : {};
      recorder = new MediaRecorder(stream, mimeOpt);
      recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);

      recorder.onstop = async () => {
        if (chunks.length > 0 && !isProcessing) {
          isProcessing = true;
          const actualMime = recorder.mimeType || mime || "audio/webm";
          const ext = actualMime.includes("mp4") || actualMime.includes("m4a") ? "m4a" : "webm";
          const blob = new Blob(chunks, { type: actualMime });
          await processAudio(blob, "audio." + ext);
        }
        if (isInCall) startRecordingLoop();
      };
  
      const checkLevel = () => {
        if (!isInCall) return;
        const vol = getVolume();
        const now = Date.now();
  
        if (state === "waiting") {
          if (vol > SPEECH_THRESHOLD) {
            state = "speech";
            speechStartTime = now;
            recorder.start(100);
          }
        } else if (state === "speech") {
          if (vol < SPEECH_THRESHOLD) {
            if (silenceStartTime === 0) silenceStartTime = now;
            if (now - speechStartTime >= MIN_SPEECH_MS && now - silenceStartTime >= SILENCE_MS) {
              recorder.stop();
              clearInterval(polling);
              return;
            }
          } else {
            silenceStartTime = 0;
          }
          if (now - speechStartTime >= MAX_RECORD_MS) {
            recorder.stop();
            clearInterval(polling);
            return;
          }
        }
      };
  
      polling = setInterval(checkLevel, POLL_MS);
    }
  
    function checkCompatibility() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        return "Microphone access is not supported. Use Chrome, Safari, or Firefox.";
      }
      if (!window.MediaRecorder) {
        return "Voice recording is not supported. Please update your browser.";
      }
      return null;
    }

    async function startCall() {
      const err = checkCompatibility();
      if (err) {
        setStatus(err);
        addEntry("agent", err);
        return;
      }
      try {
        setStatus("Connecting…", "");
        stream = await navigator.mediaDevices.getUserMedia({ audio: true, echoCancellation: true, noiseSuppression: true });
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        if (audioContext.state === "suspended") await audioContext.resume();
        const source = audioContext.createMediaStreamSource(stream);
        analyser = audioContext.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.5;
        source.connect(analyser);
        dataArray = new Uint8Array(analyser.frequencyBinCount);
  
        isInCall = true;
        startBtn.style.display = "none";
        endBtn.style.display = "inline-flex";
        setStatus("Greeting…", "speaking");
        addEntry("agent", "Hello, this is Institute of Space Technology. What is your query?");
  
        const greetingRes = await fetch("/api/greeting");
        const greetingData = await greetingRes.json();
        if (greetingData.audio) {
          await new Promise((resolve, reject) => {
            const audio = new Audio(greetingData.audio);
            audio.onended = resolve;
            audio.onerror = resolve;
            audio.play().catch(resolve);
          });
          await new Promise((r) => setTimeout(r, 400));
        }
  
        setStatus("Listening…", "listening");
        startRecordingLoop();
      } catch (e) {
        setStatus("Microphone access denied", "");
        console.error(e);
      }
    }
  
    function endCall() {
      isInCall = false;
      stopCurrentAudio();
      if (polling) clearInterval(polling);
      polling = null;
      if (recorder && recorder.state !== "inactive") recorder.stop();
      recorder = null;
      if (stream) stream.getTracks().forEach((t) => t.stop());
      stream = null;
      if (audioContext) audioContext.close();
      audioContext = null;
      analyser = null;
      dataArray = null;
  
      fetch("/api/call/end", { method: "POST" }).catch(() => {});
  
      transcriptList.innerHTML = "";
      transcriptList.appendChild(emptyState);
      emptyState.style.display = "block";
  
      startBtn.style.display = "inline-flex";
      endBtn.style.display = "none";
      setStatus("Ready — click Start to begin", "");
    }
  
    window.startCall = startCall;
    window.endCall = endCall;
  })();