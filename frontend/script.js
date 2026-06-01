/**
 * script.js — HandDance frontend game logic
 *
 * Responsibilities:
 *  - SocketIO connection management
 *  - Screen transitions (menu → game → result)
 *  - Rendering notes as DOM elements inside lanes
 *  - Displaying hit feedback (Perfect / Good / Miss)
 *  - Updating score / combo / accuracy counters
 *  - YouTube download trigger
 */

"use strict";
console.log("[HandDance] script.js v4 loaded");

// ─────────────────────────────────────────────────────────────────
// Constants (must match backend game_logic.py)
// ─────────────────────────────────────────────────────────────────
const GAME_HEIGHT_PX  = 680;
const JUDGMENT_PX     = 580;
const SCROLL_PX_S     = 360;  // pixels per second

const DIRECTION_ARROWS = { left: "←", down: "↓", up: "↑", right: "→" };
const DIRECTION_LANE   = { left: "lane-left", down: "lane-down", up: "lane-up", right: "lane-right" };
const DIRECTION_TARGET = { left: "target-left", down: "target-down", up: "target-up", right: "target-right" };

// ─────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────
let socket         = null;
let currentSong    = null;   // selected song entry
let selectedDiff   = "medium";
let noteElements   = {};     // noteId → <div>
let rafId          = null;   // requestAnimationFrame handle
let isGameActive   = false;
let lastScoreState = null;
let browserStream  = null;
let browserAudio   = null;
let browserPose    = null;
let browserCamera   = null;
let browserClockBase = 0;
let gameStartPerf = 0;
let noteRenderId = null;
let debugHandsEnabled = true;
let lastHold = null;

// ─────────────────────────────────────────────────────────────────
// DOM references
// ─────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

const screens = {
  menu:   $("menu-screen"),
  game:   $("game-screen"),
  result: $("result-screen"),
};
const connectingOverlay = $("connecting-overlay");

// Menu
const songSelect    = $("song-select");
const songInfo      = $("song-info");
const titleDisplay  = $("song-title-display");
const bpmDisplay    = $("song-bpm-display");
const notesDisplay  = $("song-notes-display");
const startBtn      = $("start-btn");
const volumeSlider  = $("volume-slider");
const volumeVal     = $("volume-val");
const ytUrl         = $("yt-url");
const ytBpm         = $("yt-bpm");
const ytDownloadBtn = $("yt-download-btn");
const ytStatus      = $("yt-status");
const camPreview    = $("cam-preview");
const camPreviewFallback = "/video_feed";
const camStatus     = $("cam-status");
const gestureStatus = $("gesture-status");
const handsOverlay  = $("hands-overlay");
const handsCtx      = handsOverlay.getContext("2d");
const streamVideo   = document.createElement("video");
streamVideo.autoplay = true;
streamVideo.playsInline = true;
streamVideo.muted = true;
const debugHandsToggle = $("debug-hands-toggle");
const debugHandsLabel = $("debug-hands-label");
const handsCountEl = $("hands-count");
const motionValueEl = $("motion-value");
const commandValueEl = $("command-value");

// Game
const playingTitle    = $("playing-title");
const scoreValue      = $("score-value");
const comboValue      = $("combo-value");
const feedbackCont    = $("feedback-container");
const statPerfect     = $("stat-perfect");
const statGood        = $("stat-good");
const statMiss        = $("stat-miss");
const statAccuracy    = $("stat-accuracy");
const statMaxCombo    = $("stat-maxcombo");
const pauseBtn        = $("pause-btn");
const quitBtn         = $("quit-btn");

// Result
const resultRank    = $("result-rank");
const resultTitle   = $("result-title");
const resScore      = $("res-score");
const resAccuracy   = $("res-accuracy");
const resMaxCombo   = $("res-maxcombo");
const resPerfect    = $("res-perfect");
const resGood       = $("res-good");
const resMiss       = $("res-miss");
const retryBtn      = $("retry-btn");
const menuBtn       = $("menu-btn");

// ─────────────────────────────────────────────────────────────────
// Screen transitions
// ─────────────────────────────────────────────────────────────────
function showScreen(name) {
  Object.entries(screens).forEach(([k, el]) => {
    el.classList.toggle("active", k === name);
  });
}

// ─────────────────────────────────────────────────────────────────
// SocketIO connection
// ─────────────────────────────────────────────────────────────────
function initSocket() {
  const host = window.location.origin;
  socket = io(host, { transports: ["websocket", "polling"] });

  socket.on("connect", () => {
    connectingOverlay.classList.add("hidden");
    console.log("Connected:", socket.id);
  });

  socket.on("disconnect", () => {
    connectingOverlay.classList.remove("hidden");
    connectingOverlay.querySelector("p").textContent = "Koneksi terputus…";
    isGameActive = false;
  });

  socket.on("connect_error", (err) => {
    connectingOverlay.classList.remove("hidden");
    connectingOverlay.querySelector("p").textContent = `Error: ${err.message}`;
  });

  // Song list refresh
  socket.on("song_list", songs => {
    populateSongList(songs);
  });

  // Game started confirmation
  socket.on("game_started", async (data) => {
    isGameActive = true;
    console.log("Game started:", data.beatmap);
    browserClockBase = data.client_start_ms || performance.now();
    gameStartPerf = performance.now();
    ensureAllNotesRendered(data.beatmap);
    await attachBrowserCamera();
    await playBrowserAudio();
    await startBrowserHands();
    startNoteRenderLoop();
  });

  // New note spawned
  socket.on("note_spawn", note => {
    spawnNote(note);
  });

  // Bulk note position update (30 Hz)
  socket.on("note_update", notes => {
    updateNotePositions(notes);
  });

  // Judgment result
  socket.on("hit_result", result => {
    showFeedback(result.rating);
    flashLane(result.direction);
    flashTarget(result.direction, result.rating);
    removeNote(result.note_id);
  });

  // Score update
  socket.on("score_update", state => {
    updateScoreDisplay(state);
    lastScoreState = state;
  });

  // Game over
  socket.on("game_over", state => {
    isGameActive = false;
    clearAllNotes();
    if (noteRenderId) cancelAnimationFrame(noteRenderId);
    noteRenderId = null;
    showResult(state);
  });

  socket.on("game_stopped", () => {
    isGameActive = false;
    clearAllNotes();
    if (noteRenderId) cancelAnimationFrame(noteRenderId);
    noteRenderId = null;
  });

  socket.on("error", msg => {
    alert("Server error: " + msg);
  });
}

async function attachBrowserCamera() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
  camStatus.textContent = "Requesting browser camera...";
  try {
    if (browserStream) {
      browserStream.getTracks().forEach(t => t.stop());
    }
    browserStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user" },
      audio: false,
    });
    camPreview.srcObject = browserStream;
    streamVideo.srcObject = browserStream;
    camPreview.classList.remove("camera-fallback");
    camStatus.textContent = "Browser camera active";
    await camPreview.play().catch(() => {});
    await streamVideo.play().catch(() => {});
  } catch (err) {
    console.warn("Camera access denied/unavailable:", err);
    camPreview.srcObject = null;
    camPreview.src = camPreviewFallback;
    camPreview.classList.add("camera-fallback");
    camStatus.textContent = "Camera unavailable, showing fallback";
  }
}

async function startBrowserHands() {
  if (!browserStream || browserPose) return;

  const PoseCtor = window.Pose || window.pose?.Pose || null;
  if (!PoseCtor) {
    camStatus.textContent = "Pose tracker library not loaded";
    return;
  }

  browserPose = new PoseCtor({
    locateFile: (file) => `https://cdn.jsdelivr.net/npm/@mediapipe/pose/${file}`,
  });
  browserPose.setOptions({
    modelComplexity: 0,
    smoothLandmarks: true,
    enableSegmentation: false,
    smoothSegmentation: false,
    minDetectionConfidence: 0.7,
    minTrackingConfidence: 0.6,
  });

  let lastClockSent = 0;
  let lastHold = null;

  browserPose.onResults((results) => {
    const now = performance.now();
    if (socket && socket.connected && now - lastClockSent > 200) {
      socket.emit("client_clock", { ms: now - browserClockBase });
      lastClockSent = now;
    }

    const poseLandmarks = results.poseLandmarks || [];
    handsCountEl.textContent = `pose: ${poseLandmarks.length ? 1 : 0}`;

    if (!poseLandmarks.length) {
      gestureStatus.textContent = "Gesture: none";
      motionValueEl.textContent = "motion: --";
      commandValueEl.textContent = "command: --";
      drawHandsOverlay(results);
      return;
    }

    drawHandsOverlay(results);

    const command = classifyPoseHold(poseLandmarks);
    motionValueEl.textContent = `motion: ${command.confidence.toFixed(2)}`;
    commandValueEl.textContent = `command: ${command.command || "none"}`;

    if (command.command && command.command !== lastHold) {
      lastHold = command.command;
      gestureStatus.textContent = `Gesture: ${command.command}`;
      socket.emit("gesture", { direction: command.command, timestamp_ms: now - browserClockBase });
    } else if (!command.command) {
      lastHold = null;
      gestureStatus.textContent = "Gesture: none";
    }
  });

  const pump = async () => {
    if (!isGameActive || !browserPose || !browserStream) return;
    try {
      await browserPose.send({ image: streamVideo });
    } catch (err) {
      console.warn("Pose send failed:", err);
    }
    browserCamera = requestAnimationFrame(pump);
  };
  browserCamera = requestAnimationFrame(pump);
}

function classifyPoseHold(poseLandmarks) {
  const nose = poseLandmarks[0];
  const leftShoulder = poseLandmarks[11];
  const rightShoulder = poseLandmarks[12];
  const leftWrist = poseLandmarks[15];
  const rightWrist = poseLandmarks[16];
  const leftHip = poseLandmarks[23];
  const rightHip = poseLandmarks[24];

  if (!nose || !leftShoulder || !rightShoulder || !leftWrist || !rightWrist) {
    return { command: null, confidence: 0 };
  }

  const headX = nose.x;
  const shoulderY = (leftShoulder.y + rightShoulder.y) / 2;
  const hipY = (leftHip && rightHip) ? (leftHip.y + rightHip.y) / 2 : shoulderY + 0.25;

  const leftDistance = headX - leftWrist.x;
  const rightDistance = rightWrist.x - headX;
  const upDistance = shoulderY - Math.min(leftWrist.y, rightWrist.y);
  const downDistance = Math.max(leftWrist.y, rightWrist.y) - hipY;

  if (leftDistance > 0.10) return { command: "left", confidence: leftDistance };
  if (rightDistance > 0.10) return { command: "right", confidence: rightDistance };
  if (upDistance > 0.10) return { command: "up", confidence: upDistance };
  if (downDistance > 0.02) return { command: "down", confidence: downDistance };

  return { command: null, confidence: 0 };
}

function drawHandsOverlay(results) {
  const width = handsOverlay.width = camPreview.clientWidth || 320;
  const height = handsOverlay.height = camPreview.clientHeight || 240;
  handsCtx.clearRect(0, 0, width, height);

  const poseLandmarks = results ? results.poseLandmarks : null;
  if (!poseLandmarks || poseLandmarks.length === 0) {
    handsCtx.fillStyle = "rgba(255,255,255,0.8)";
    handsCtx.font = "16px sans-serif";
    handsCtx.fillText("No pose detected", 16, 26);
    return;
  }

  const landmarks = poseLandmarks;
  handsCtx.strokeStyle = "#00ff9d";
  handsCtx.fillStyle = "#00ff9d";
  handsCtx.lineWidth = 3;

  const points = landmarks.map((lm, idx) => ({
    x: lm.x * width,
    y: lm.y * height,
    idx,
  }));
  const connections = [
    [0,11],[11,12],[12,24],[24,23],
    [11,13],[13,15],[12,14],[14,16],
    [23,25],[25,27],[24,26],[26,28],
    [0,1],[1,2],[2,3]
  ];

  handsCtx.beginPath();
  connections.forEach(([a, b]) => {
    const pa = points[a];
    const pb = points[b];
    if (!pa || !pb) return;
    handsCtx.moveTo(pa.x, pa.y);
    handsCtx.lineTo(pb.x, pb.y);
  });
  handsCtx.stroke();

  points.forEach(p => {
    handsCtx.beginPath();
    handsCtx.arc(p.x, p.y, p.idx === 0 ? 7 : 4, 0, Math.PI * 2);
    handsCtx.fill();
  });

  const nose = points[0];
  if (nose) {
    handsCtx.fillStyle = "rgba(255,255,255,0.9)";
    handsCtx.fillText("pose detected", nose.x + 10, nose.y - 10);
  }
}

function startNoteRenderLoop() {
  if (noteRenderId) cancelAnimationFrame(noteRenderId);

  const render = () => {
    if (!isGameActive) return;
    const nowMs = performance.now() - gameStartPerf;

    Object.values(noteElements).forEach((el) => {
      if (!el || !el.dataset.timeMs) return;
      const noteTime = parseFloat(el.dataset.timeMs);
      const y = JUDGMENT_PX - ((noteTime - nowMs) / 1000) * SCROLL_PX_S;
      el.style.top = `${Math.round(y)}px`;
      el.style.transform = "translateX(-50%)";
    });

    noteRenderId = requestAnimationFrame(render);
  };

  noteRenderId = requestAnimationFrame(render);
}

async function playBrowserAudio() {
  if (!currentSong) return;
  const audioUrl = `/music/${encodeURIComponent(currentSong.audio_file)}`;

  if (browserAudio) {
    browserAudio.pause();
    browserAudio = null;
  }

  browserAudio = new Audio(audioUrl);
  browserAudio.preload = "auto";
  browserAudio.crossOrigin = "anonymous";

  try {
    await browserAudio.play();
  } catch (err) {
    console.warn("Browser audio play failed:", err);
  }
}

// ─────────────────────────────────────────────────────────────────
// Song list
// ─────────────────────────────────────────────────────────────────
let _songs = [];

function populateSongList(songs) {
  _songs = songs;
  const prev = songSelect.value;
  songSelect.innerHTML = "";

  if (!songs || songs.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "-- Tidak ada lagu. Tambah via YouTube --";
    songSelect.appendChild(opt);
    startBtn.disabled = true;
    return;
  }

  songs.forEach(song => {
    const opt = document.createElement("option");
    opt.value = song.audio_file;
    const hasBM = song.has_beatmap ? "" : " (auto)";
    opt.textContent = (song.title || song.audio_file) + hasBM;
    songSelect.appendChild(opt);
  });

  // Restore previous selection if still available
  if (prev && [...songSelect.options].some(o => o.value === prev)) {
    songSelect.value = prev;
  }

  onSongChange();
  startBtn.disabled = !songSelect.value;
}

function onSongChange() {
  const val  = songSelect.value;
  const song = _songs.find(s => s.audio_file === val);
  currentSong = song || null;

  if (song && song.has_beatmap) {
    titleDisplay.textContent  = song.title  || val;
    bpmDisplay.textContent    = song.bpm    || "–";
    notesDisplay.textContent  = song.note_count || "–";
    songInfo.classList.remove("hidden");
  } else if (song) {
    titleDisplay.textContent  = song.audio_file;
    bpmDisplay.textContent    = "auto";
    notesDisplay.textContent  = "auto";
    songInfo.classList.remove("hidden");
  } else {
    songInfo.classList.add("hidden");
  }

  startBtn.disabled = !val;
}

// ─────────────────────────────────────────────────────────────────
// Note rendering
// ─────────────────────────────────────────────────────────────────
function spawnNote(note) {
  const laneId = DIRECTION_LANE[note.direction];
  const lane   = $(laneId);
  if (!lane) return;

  const div = document.createElement("div");
  div.className = "note";
  div.id        = "note-" + note.id;
  div.style.top = "-70px";  // start above screen
  div.dataset.timeMs = note.time_ms;
  div.setAttribute("data-dir", note.direction);
  div.textContent = DIRECTION_ARROWS[note.direction];
  lane.appendChild(div);

  noteElements[note.id] = div;
}

function updateNotePositions(notes) {
  notes.forEach(note => {
    const el = noteElements[note.id];
    if (!el) return;
    if (note.hit || note.missed) {
      el.classList.add(note.hit ? "hit" : "missed");
      return;
    }
    el.dataset.timeMs = note.time_ms;
  });
}

function ensureAllNotesRendered(beatmap) {
  if (!beatmap || !beatmap.notes) return;
  beatmap.notes.forEach((note, idx) => {
    if (noteElements[note.id]) return;
    spawnNote(note);
    const el = noteElements[note.id];
    if (el) {
      el.dataset.timeMs = note.time_ms;
      el.dataset.index = String(idx);
    }
  });
}

function removeNote(noteId) {
  const el = noteElements[noteId];
  if (!el) return;
  el.classList.add("hit");
  setTimeout(() => {
    el.remove();
    delete noteElements[noteId];
  }, 200);
}

function clearAllNotes() {
  Object.values(noteElements).forEach(el => el.remove());
  noteElements = {};
}

// ─────────────────────────────────────────────────────────────────
// Feedback / visuals
// ─────────────────────────────────────────────────────────────────
function showFeedback(rating) {
  const labels = {
    perfect: "PERFECT",
    good:    "GOOD",
    miss:    "MISS",
    wrong:   "WRONG",
  };
  const span = document.createElement("span");
  span.className   = `feedback-text ${rating}`;
  span.textContent = labels[rating] || rating.toUpperCase();
  feedbackCont.appendChild(span);
  setTimeout(() => span.remove(), 600);
}

function flashLane(direction) {
  const lane = $(DIRECTION_LANE[direction]);
  if (!lane) return;
  lane.classList.remove("flash");
  void lane.offsetWidth; // reflow to restart animation
  lane.classList.add("flash");
  setTimeout(() => lane.classList.remove("flash"), 300);
}

function flashTarget(direction, rating) {
  if (rating === "wrong") return;
  const target = $(DIRECTION_TARGET[direction]);
  if (!target) return;
  target.classList.add("hit");
  setTimeout(() => target.classList.remove("hit"), 200);
}

// ─────────────────────────────────────────────────────────────────
// Score display
// ─────────────────────────────────────────────────────────────────
function updateScoreDisplay(state) {
  scoreValue.textContent   = state.score;
  comboValue.textContent   = state.combo;
  statPerfect.textContent  = state.perfects;
  statGood.textContent     = state.goods;
  statMiss.textContent     = state.misses;
  statAccuracy.textContent = state.accuracy + "%";
  statMaxCombo.textContent = state.max_combo;

  // Pop animation on combo increase
  if (state.combo > 0) {
    comboValue.classList.remove("combo-pop");
    void comboValue.offsetWidth;
    comboValue.classList.add("combo-pop");
  }
}

// ─────────────────────────────────────────────────────────────────
// Result screen
// ─────────────────────────────────────────────────────────────────
function showResult(state) {
  const acc = state.accuracy;
  let rankChar, rankClass;
  if      (acc >= 95) { rankChar = "S"; rankClass = "s"; }
  else if (acc >= 85) { rankChar = "A"; rankClass = "a"; }
  else if (acc >= 70) { rankChar = "B"; rankClass = "b"; }
  else if (acc >= 50) { rankChar = "C"; rankClass = "c"; }
  else                { rankChar = "D"; rankClass = "d"; }

  resultRank.textContent = rankChar;
  resultRank.className   = `rank ${rankClass}`;
  resultTitle.textContent = currentSong
    ? (currentSong.title || currentSong.audio_file)
    : "–";

  resScore.textContent    = state.score;
  resAccuracy.textContent = acc + "%";
  resMaxCombo.textContent = state.max_combo;
  resPerfect.textContent  = state.perfects;
  resGood.textContent     = state.goods;
  resMiss.textContent     = state.misses;

  showScreen("result");
}

// ─────────────────────────────────────────────────────────────────
// Event wiring
// ─────────────────────────────────────────────────────────────────
function wireEvents() {
  if (debugHandsToggle) {
    debugHandsToggle.checked = true;
    debugHandsLabel.textContent = "ON";
    debugHandsToggle.addEventListener("change", () => {
      debugHandsEnabled = debugHandsToggle.checked;
      debugHandsLabel.textContent = debugHandsEnabled ? "ON" : "OFF";
      if (!debugHandsEnabled) {
        handsCtx.clearRect(0, 0, handsOverlay.width, handsOverlay.height);
      }
    });
  }

  // Difficulty buttons
  document.querySelectorAll(".diff-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".diff-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      selectedDiff = btn.dataset.diff;
    });
  });

  // Song select change
  songSelect.addEventListener("change", onSongChange);

  // Volume
  volumeSlider.addEventListener("input", () => {
    const v = volumeSlider.value;
    volumeVal.textContent = v + "%";
    if (socket && socket.connected) {
      socket.emit("set_volume", { volume: v / 100 });
    }
  });

  // Start button
  startBtn.addEventListener("click", () => {
    const file = songSelect.value;
    if (!file) return;

    // Reset game display
    scoreValue.textContent  = "0";
    comboValue.textContent  = "0";
    statPerfect.textContent = "0";
    statGood.textContent    = "0";
    statMiss.textContent    = "0";
    statAccuracy.textContent = "100%";
    statMaxCombo.textContent = "0";
    clearAllNotes();
    noteElements = {};
    feedbackCont.innerHTML = "";

    playingTitle.textContent = currentSong
      ? (currentSong.title || currentSong.audio_file)
      : file;

    camStatus.textContent = "Starting...";

    if (browserAudio) {
      browserAudio.pause();
      browserAudio = null;
    }

    // Start audio immediately on user gesture, then sync the game state.
    playBrowserAudio();

    socket.emit("start_game", {
      audio_file: file,
      difficulty: selectedDiff,
      bpm: currentSong && currentSong.bpm ? currentSong.bpm : 120,
    });

    showScreen("game");
  });

  // Pause / Resume
  let paused = false;
  pauseBtn.addEventListener("click", () => {
    paused = !paused;
    pauseBtn.textContent = paused ? "▶" : "II";
    // TODO: backend pause support (currently stops audio)
  });

  // Quit
  quitBtn.addEventListener("click", () => {
    socket.emit("stop_game");
    clearAllNotes();
    if (browserStream) {
      browserStream.getTracks().forEach(t => t.stop());
      browserStream = null;
      camPreview.srcObject = null;
    }
    if (browserAudio) {
      browserAudio.pause();
      browserAudio = null;
    }
    if (browserCamera) {
      cancelAnimationFrame(browserCamera);
      browserCamera = null;
    }
    browserPose = null;
    handsCtx.clearRect(0, 0, handsOverlay.width, handsOverlay.height);
    camStatus.textContent = "Browser camera loading...";
    showScreen("menu");
  });

  // Result: Retry
  retryBtn.addEventListener("click", () => {
    showScreen("menu");
    // Re-trigger start automatically
    setTimeout(() => startBtn.click(), 100);
  });

  // Result: Menu
  menuBtn.addEventListener("click", () => {
    showScreen("menu");
  });

  // YouTube download
  ytDownloadBtn.addEventListener("click", async () => {
    const url = ytUrl.value.trim();
    if (!url) { showYtStatus("Masukkan URL YouTube.", "err"); return; }

    ytDownloadBtn.disabled = true;
    showYtStatus("Mengunduh audio… harap tunggu.", "loading");

    try {
      const res  = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, bpm: parseFloat(ytBpm.value) || 120 }),
      });
      const data = await res.json();
      if (data.error) {
        showYtStatus("Error: " + data.error, "err");
      } else {
        showYtStatus(`Berhasil: "${data.title}" ditambahkan!`, "ok");
        ytUrl.value = "";
      }
    } catch (e) {
      showYtStatus("Koneksi gagal: " + e.message, "err");
    } finally {
      ytDownloadBtn.disabled = false;
    }
  });
}

function showYtStatus(msg, cls) {
  ytStatus.textContent = msg;
  ytStatus.className = "yt-status " + cls;
}

// ─────────────────────────────────────────────────────────────────
// Boot
// ─────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  showScreen("menu");
  connectingOverlay.classList.remove("hidden");
  wireEvents();
  initSocket();
});
