(function () {
    /** MediaRecorder fallback: long pause tolerance so full questions are captured */
    const SPEECH_THRESHOLD = 18;
    const SILENCE_MS = 1800;
    const MIN_SPEECH_MS = 400;
    const MAX_RECORD_MS = 30000;
    const POLL_MS = 80;

    /** Web Speech: send after this quiet period following last final result */
    const SPEECH_FINAL_DEBOUNCE_MS = 2000;

    let stream = null;
    let audioContext = null;
    let analyser = null;
    let recorder = null;
    let polling = null;
    let isInCall = false;
    let isProcessing = false;
    let isSpeaking = false;
    let dataArray = null;
    let currentAudio = null;
    let interruptCheck = null;
    let timeDomainBuf = null;
    let speechRec = null;
    let speechDebounceTimer = null;
    let pendingSpeechText = "";

    const SpeechRecognition =
      typeof window !== "undefined" && (window.SpeechRecognition || window.webkitSpeechRecognition);
    const useSpeechRecognition =
      !!SpeechRecognition && (window.isSecureContext === true || location.hostname === "localhost" || location.hostname === "127.0.0.1");

    /** Interrupt during TTS: voice-band + adaptive baseline (works with echo cancellation) */
    const VAD_POLL_MS = 80;
    const VAD_SKIP_MS = 350;
    const VAD_DELTA_THRESHOLD = 14;
    const VAD_ABS_MIN = 22;
    const VAD_SUSTAIN_MS = 380;
    const VAD_SMOOTH = 0.08;

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
      div.innerHTML = '<span class="role">' + role + '</span><div class="content">' + escapeHtml(content) + "</div>";
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

    /** ~280 Hz–3.8 kHz voice band (bins depend on sample rate and fftSize) */
    function getVoiceBandLevel() {
      if (!analyser || !dataArray || !audioContext) return 0;
      analyser.getByteFrequencyData(dataArray);
      const sr = audioContext.sampleRate || 44100;
      const hzPerBin = sr / analyser.fftSize;
      const i0 = Math.max(2, Math.floor(280 / hzPerBin));
      const i1 = Math.min(dataArray.length - 1, Math.ceil(3800 / hzPerBin));
      if (i0 > i1) return 0;
      let sum = 0;
      for (let i = i0; i <= i1; i++) sum += dataArray[i];
      return sum / (i1 - i0 + 1);
    }

    function getTimeDomainVariance() {
      if (!analyser) return 0;
      if (!timeDomainBuf) timeDomainBuf = new Uint8Array(analyser.fftSize);
      analyser.getByteTimeDomainData(timeDomainBuf);
      let mean = 0;
      for (let i = 0; i < timeDomainBuf.length; i++) mean += timeDomainBuf[i];
      mean /= timeDomainBuf.length;
      let v = 0;
      for (let i = 0; i < timeDomainBuf.length; i++) {
        const d = timeDomainBuf[i] - mean;
        v += d * d;
      }
      return v / timeDomainBuf.length / (255 * 255);
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

    function stopSpeechRecognition() {
      if (!speechRec) return;
      try {
        speechRec.stop();
      } catch (e) {
        /* already stopped */
      }
    }

    function abortSpeechDebounce() {
      if (speechDebounceTimer) {
        clearTimeout(speechDebounceTimer);
        speechDebounceTimer = null;
      }
      pendingSpeechText = "";
    }

    function getAudioFilename(blob) {
      const t = blob && blob.type ? blob.type.toLowerCase() : "";
      if (t.includes("webm")) return "audio.webm";
      if (t.includes("mp4") || t.includes("m4a")) return "audio.m4a";
      if (t.includes("ogg")) return "audio.ogg";
      return "audio.webm";
    }

    async function finishTurn(data) {
      isProcessing = false;
      if (!isInCall) return;
      if (data && data.end_call) return;
      setStatus("Listening…", "listening");
      if (useSpeechRecognition && speechRec) {
        try {
          speechRec.start();
        } catch (e) {
          /* already running; onend will restart */
        }
      } else {
        startRecordingLoop();
      }
    }

    /** Shared TTS + barge-in (voice-band delta + variance) */
    async function playAgentAudio(audioUrl) {
      setStatus("Speaking…", "speaking");
      isSpeaking = true;
      stopSpeechRecognition();
      abortSpeechDebounce();

      const audio = new Audio(audioUrl);
      currentAudio = audio;
      let resolvePlay = function () {};
      const playDone = new Promise(function (resolve) {
        resolvePlay = resolve;
      });

      let vbBaseline = 0;
      const ttsStart = Date.now();
      let sustainedStart = null;

      interruptCheck = setInterval(function () {
        if (!isInCall || !currentAudio) return;
        const now = Date.now();
        if (now - ttsStart < VAD_SKIP_MS) return;

        const vb = getVoiceBandLevel();
        if (vbBaseline === 0) vbBaseline = vb;
        else vbBaseline = (1 - VAD_SMOOTH) * vbBaseline + VAD_SMOOTH * vb;
        const delta = vb - vbBaseline;
        const variance = getTimeDomainVariance();
        const looksLikeUserSpeech =
          vb > VAD_ABS_MIN &&
          delta > VAD_DELTA_THRESHOLD &&
          variance > 0.003;

        if (looksLikeUserSpeech) {
          if (sustainedStart === null) sustainedStart = now;
          else if (now - sustainedStart >= VAD_SUSTAIN_MS) {
            clearInterval(interruptCheck);
            interruptCheck = null;
            stopCurrentAudio();
            isSpeaking = false;
            resolvePlay();
          }
        } else {
          sustainedStart = null;
        }
      }, VAD_POLL_MS);

      await Promise.race([
        playDone,
        new Promise(function (resolve) {
          audio.onended = function () {
            stopCurrentAudio();
            isSpeaking = false;
            resolve();
          };
          audio.onerror = function () {
            stopCurrentAudio();
            isSpeaking = false;
            resolve();
          };
          audio.play().catch(function () {
            isSpeaking = false;
            resolve();
          });
        }),
      ]);

      if (interruptCheck) {
        clearInterval(interruptCheck);
        interruptCheck = null;
      }
      if (isSpeaking) isSpeaking = false;
    }

    async function handleProcessResult(data) {
      if (data.transcript && !data.transcript.toLowerCase().includes("sorry")) {
        addEntry("you", data.transcript);
      }
      if (data.reply) {
        addEntry("agent", data.reply);
        if (data.audio) {
          await playAgentAudio(data.audio);
        }
        if (data.end_call) {
          isInCall = false;
          setTimeout(endCall, 500);
          return;
        }
      } else if (!data.reply) {
        addEntry("agent", "Could not understand. Please try again.");
      }
    }

    async function processText(text) {
      const trimmed = (text || "").trim();
      if (!trimmed || trimmed.length < 2) {
        await finishTurn({});
        return;
      }
      isProcessing = true;
      stopSpeechRecognition();
      abortSpeechDebounce();
      setStatus("Processing…", "processing");

      let data = {};
      try {
        const res = await fetch("/api/call/process", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ text: trimmed }),
        });
        if (!res.ok) {
          addEntry("agent", "Server error. Please try again.");
          await finishTurn({});
          return;
        }
        data = await res.json();
        await handleProcessResult(data);
      } catch (e) {
        console.error(e);
        addEntry("agent", "Something went wrong. Please try again.");
      }
      await finishTurn(data);
    }

    async function processAudio(blob, filename) {
      if (!blob || blob.size < 500) {
        await finishTurn({});
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
          await finishTurn({});
          return;
        }
        data = await res.json();
        await handleProcessResult(data);
      } catch (e) {
        console.error(e);
        addEntry("agent", "Something went wrong. Please try again.");
      }
      await finishTurn(data);
    }

    function initSpeechRecognition() {
      if (!useSpeechRecognition || speechRec) return;
      speechRec = new SpeechRecognition();
      speechRec.continuous = true;
      speechRec.interimResults = true;
      speechRec.lang = "en-US";
      speechRec.maxAlternatives = 1;

      speechRec.onresult = function (e) {
        if (!isInCall || isProcessing || isSpeaking) return;
        let piece = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          if (e.results[i].isFinal) piece += e.results[i][0].transcript;
        }
        if (piece) pendingSpeechText += piece;
        clearTimeout(speechDebounceTimer);
        speechDebounceTimer = setTimeout(function () {
          const t = pendingSpeechText.trim();
          pendingSpeechText = "";
          speechDebounceTimer = null;
          if (t.length >= 3 && isInCall && !isProcessing && !isSpeaking) {
            processText(t);
          }
        }, SPEECH_FINAL_DEBOUNCE_MS);
      };

      speechRec.onerror = function (ev) {
        if (ev.error === "aborted" || ev.error === "no-speech") return;
        console.warn("Speech recognition:", ev.error);
      };

      speechRec.onend = function () {
        if (isInCall && !isProcessing && !isSpeaking) {
          try {
            speechRec.start();
          } catch (err) {
            /* ignore */
          }
        }
      };
    }

    function startRecordingLoop() {
      let chunks = [];
      let speechStartTime = 0;
      let silenceStartTime = 0;
      let state = "waiting";

      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : MediaRecorder.isTypeSupported("audio/webm")
          ? "audio/webm"
          : MediaRecorder.isTypeSupported("audio/mp4")
            ? "audio/mp4"
            : MediaRecorder.isTypeSupported("audio/mpeg")
              ? "audio/mpeg"
              : "";
      const mimeOpt = mime ? { mimeType: mime } : {};
      recorder = new MediaRecorder(stream, mimeOpt);
      recorder.ondataavailable = function (e) {
        if (e.data.size) chunks.push(e.data);
      };

      recorder.onstop = async function () {
        if (chunks.length > 0 && !isProcessing) {
          isProcessing = true;
          const actualMime = recorder.mimeType || mime || "audio/webm";
          const ext = actualMime.includes("mp4") || actualMime.includes("m4a") ? "m4a" : "webm";
          const blob = new Blob(chunks, { type: actualMime });
          await processAudio(blob, "audio." + ext);
        } else if (isInCall && !isProcessing) {
          await finishTurn({});
        }
      };

      const checkLevel = function () {
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
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        if (audioContext.state === "suspended") await audioContext.resume();
        const source = audioContext.createMediaStreamSource(stream);
        analyser = audioContext.createAnalyser();
        analyser.fftSize = 2048;
        analyser.smoothingTimeConstant = 0.65;
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
          isSpeaking = true;
          await new Promise(function (resolve) {
            const audio = new Audio(greetingData.audio);
            audio.onended = resolve;
            audio.onerror = resolve;
            audio.play().catch(resolve);
          });
          isSpeaking = false;
          await new Promise(function (r) {
            setTimeout(r, 400);
          });
        }

        initSpeechRecognition();
        setStatus("Listening…", "listening");
        if (useSpeechRecognition && speechRec) {
          try {
            speechRec.start();
          } catch (e) {
            startRecordingLoop();
          }
        } else {
          startRecordingLoop();
        }
      } catch (e) {
        setStatus("Microphone access denied", "");
        console.error(e);
      }
    }

    function endCall() {
      isInCall = false;
      isSpeaking = false;
      stopCurrentAudio();
      abortSpeechDebounce();
      if (speechRec) {
        try {
          speechRec.onend = null;
          speechRec.stop();
        } catch (e) {
          /* ignore */
        }
      }
      if (polling) clearInterval(polling);
      polling = null;
      if (recorder && recorder.state !== "inactive") recorder.stop();
      recorder = null;
      if (stream) stream.getTracks().forEach(function (t) {
        t.stop();
      });
      stream = null;
      if (audioContext) audioContext.close();
      audioContext = null;
      analyser = null;
      dataArray = null;

      fetch("/api/call/end", { method: "POST" }).catch(function () {});

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
