"""
Main entry point — Flask-SocketIO server.

Routes:
  GET  /                → index.html
  GET  /static/<path>   → frontend static files
  GET  /api/songs       → list available audio files
  GET  /api/beatmaps    → list available beatmaps
  GET  /video_feed      → MJPEG stream from hand-tracker camera
  POST /api/download    → (yt-dlp) download audio from YouTube URL

SocketIO events (server → client):
  song_list        – list of songs + beatmaps
  game_started     – beatmap metadata when game begins
  note_spawn       – { id, direction, time_ms }
  note_update      – list of active notes with current y_px (60 Hz)
  hit_result       – { note_id, direction, rating, points, diff_ms }
  score_update     – full GameState dict
  game_over        – final score summary
  error            – error message string

SocketIO events (client → server):
  start_game       – { audio_file, difficulty }
  stop_game        – stop current session
  set_volume       – { volume: 0.0–1.0 }
"""

# gevent monkey-patch MUST happen before any other import so that
# stdlib sockets, threading, and ssl all become gevent-cooperative.
from gevent import monkey
monkey.patch_all()

import os
import sys
import logging
import threading
import time
from urllib.parse import unquote

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit

# Resolve paths relative to this file so imports work inside Docker
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
FRONT_DIR = os.path.join(BASE_DIR, "..", "frontend")
MUSIC_DIR = os.environ.get("MUSIC_DIR", os.path.join(BASE_DIR, "music"))
BEATMAP_DIR = os.environ.get("BEATMAP_DIR", os.path.join(BASE_DIR, "beatmaps"))

sys.path.insert(0, BASE_DIR)

from hand_tracker   import HandTracker
from audio_manager  import AudioManager
from beatmap_parser import BeatmapParser
from game_logic     import GameSession

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

# ------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------
app = Flask(
    __name__,
    template_folder = FRONT_DIR,
    static_folder   = FRONT_DIR,
    static_url_path = "/static",
)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dance-hand-secret")
socketio = SocketIO(
    app,
    cors_allowed_origins = "*",
    async_mode          = "gevent",
    logger              = False,
    engineio_logger     = False,
)

# ------------------------------------------------------------------
# Singletons
# ------------------------------------------------------------------
hand_tracker   = HandTracker(
    camera_index    = int(os.environ.get("CAMERA_INDEX", "0")),
    swipe_threshold = float(os.environ.get("SWIPE_THRESHOLD", "0.12")),
)
audio_manager  = AudioManager(music_dir=MUSIC_DIR)
beatmap_parser = BeatmapParser(beatmap_dir=BEATMAP_DIR)

current_session: GameSession | None = None
session_lock = threading.Lock()
client_clock_ms: float = 0.0
client_clock_started_at: float = 0.0

# ------------------------------------------------------------------
# HTTP routes
# ------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(FRONT_DIR, "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(FRONT_DIR, filename)

@app.route("/api/songs")
def api_songs():
    songs    = audio_manager.list_songs()
    beatmaps = {bm["audio_file"]: bm for bm in beatmap_parser.list_beatmaps()}
    result   = []
    for song in songs:
        entry = {"audio_file": song, "has_beatmap": song in beatmaps}
        if song in beatmaps:
            entry.update(beatmaps[song])
        result.append(entry)
    return jsonify(result)


@app.route("/music/<path:filename>")
def music_file(filename):
    return send_from_directory(MUSIC_DIR, filename)

@app.route("/api/beatmaps")
def api_beatmaps():
    return jsonify(beatmap_parser.list_beatmaps())

@app.route("/video_feed")
def video_feed():
    """MJPEG stream of the annotated camera frame."""
    def generate():
        while True:
            jpeg = hand_tracker.get_frame_jpeg()
            if jpeg:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
            time.sleep(1 / 30)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )

@app.route("/api/download", methods=["POST"])
def api_download():
    """Download audio from a YouTube URL using yt-dlp."""
    data = request.get_json(force=True)
    url  = data.get("url", "").strip()
    bpm  = float(data.get("bpm", 120))

    if not url:
        return jsonify({"error": "No URL provided."}), 400

    try:
        import yt_dlp  # type: ignore
    except ImportError:
        return jsonify({"error": "yt-dlp not installed."}), 501

    out_template = os.path.join(MUSIC_DIR, "%(id)s.%(ext)s")
    ydl_opts = {
        "format":           "bestaudio/best",
        "outtmpl":          out_template,
        "noplaylist":       True,
        "quiet":            True,
        "restrictfilenames": True,
        "postprocessors": [{
            "key":            "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title    = info.get("title", "unknown")
            video_id = info.get("id", "unknown")
            filename = video_id + ".mp3"

        # Auto-generate beatmap
        duration_ms = info.get("duration", 180) * 1000
        bm = beatmap_parser.generate_auto_beatmap(
            audio_filename=filename,
            bpm=bpm,
            duration_ms=duration_ms,
        )
        _save_auto_beatmap(bm, filename, title=title)

        # Notify all clients
        socketio.emit("song_list", _build_song_list())
        return jsonify({"success": True, "filename": filename, "title": title})

    except Exception as exc:
        logger.error("yt-dlp error: %s", exc)
        return jsonify({"error": str(exc)}), 500


def _save_auto_beatmap(bm, audio_filename: str, title: str | None = None):
    """Persist an auto-generated beatmap as JSON."""
    import json
    stem = os.path.splitext(audio_filename)[0]
    path = os.path.join(BEATMAP_DIR, stem + ".json")
    os.makedirs(BEATMAP_DIR, exist_ok=True)
    data = {
        "title":      title or bm.title,
        "artist":     bm.artist,
        "audio_file": bm.audio_file,
        "bpm":        bm.bpm,
        "offset":     bm.offset_ms / 1000,
        "difficulty": bm.difficulty,
        "notes":      [{"time": n.time_ms / 1000, "direction": n.direction}
                       for n in bm.notes],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    logger.info("Auto-beatmap saved: %s", path)


def _build_song_list():
    songs    = audio_manager.list_songs()
    beatmaps = {bm["audio_file"]: bm for bm in beatmap_parser.list_beatmaps()}
    result   = []
    for song in songs:
        entry = {"audio_file": song, "has_beatmap": song in beatmaps}
        if song in beatmaps:
            entry.update(beatmaps[song])
        result.append(entry)
    return result


def _resolve_audio_filename(requested: str) -> str:
    """Resolve a possibly encoded filename against files in MUSIC_DIR."""
    if not requested:
        return ""

    requested = unquote(requested).strip()
    available = audio_manager.list_songs()
    if requested in available:
        return requested

    requested_base = os.path.basename(requested)
    if requested_base in available:
        return requested_base

    requested_cf = requested.casefold()
    requested_base_cf = requested_base.casefold()
    for song in available:
        if song.casefold() in {requested_cf, requested_base_cf}:
            return song

    for song in available:
        if os.path.basename(song).casefold() == requested_base_cf:
            return song

    logger.error("Audio file not found. Requested=%r Available=%r", requested, available)
    return ""

# ------------------------------------------------------------------
# SocketIO events
# ------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    logger.info("Client connected: %s", request.sid)
    emit("song_list", _build_song_list())


@socketio.on("disconnect")
def on_disconnect():
    logger.info("Client disconnected: %s", request.sid)


@socketio.on("start_game")
def on_start_game(data):
    global current_session
    global client_clock_ms, client_clock_started_at

    requested_audio = str(data.get("audio_file", ""))
    audio_file = _resolve_audio_filename(requested_audio)
    difficulty = data.get("difficulty", "medium")
    bpm_override = float(data.get("bpm", 0))

    # Stop any running session
    with session_lock:
        if current_session:
            current_session.stop()
            current_session = None

    client_clock_started_at = time.time() * 1000
    client_clock_ms = 0.0

    # Verify file exists in mounted music folder. Playback happens in browser.
    if not audio_file:
        emit("error", f"Cannot load audio file: {requested_audio}")
        return

    # Keep audio_manager in sync for timing only; if audio device is missing,
    # we still allow game start and use browser playback.
    audio_manager.load(audio_file)

    # Load or auto-generate beatmap
    beatmap = beatmap_parser.load_for_audio(audio_file)
    if beatmap is None:
        bpm = bpm_override or 120
        dur = audio_manager.get_duration_ms() or 180_000
        logger.info("No beatmap found for '%s', auto-generating (BPM=%.0f).", audio_file, bpm)
        beatmap = beatmap_parser.generate_auto_beatmap(
            audio_filename=audio_file,
            bpm=bpm,
            duration_ms=dur,
            difficulty=difficulty,
        )

    # SocketIO callbacks (run from game thread → emit to all clients)
    def on_note_spawn(note: dict):
        socketio.emit("note_spawn", note)

    def on_hit_result(result: dict, state: dict):
        socketio.emit("hit_result", result)
        socketio.emit("score_update", state)

    def on_game_end(state: dict):
        socketio.emit("game_over", state)

    # Create and start session
    session = GameSession(
        beatmap        = beatmap,
        audio_manager  = audio_manager,
        hand_tracker   = hand_tracker,
        on_note_spawn  = on_note_spawn,
        on_hit_result  = on_hit_result,
        on_game_end    = on_game_end,
    )

    with session_lock:
        current_session = session

    session.set_external_clock(lambda: client_clock_ms)

    session.start()

    # Confirm to client
    emit("game_started", {
        "beatmap":   beatmap.to_dict(),
        "scroll_px": 360,            # SCROLL_SPEED_PX_PER_S
        "height_px": 680,            # GAME_HEIGHT_PX
        "judgment_px": 580,          # JUDGMENT_LINE_PX
        "server_start_ms": client_clock_started_at,
    })
    logger.info("Game started: '%s' (%s)", audio_file, difficulty)

    # Kick off note-position push thread
    _start_note_push_thread(session)


def _start_note_push_thread(session: GameSession):
    """Push active note positions to clients at ~30 FPS."""
    def pusher():
        while session._running:
            notes = session.get_active_notes()
            socketio.emit("note_update", notes)
            time.sleep(1 / 30)

    t = threading.Thread(target=pusher, daemon=True, name="NotePusher")
    t.start()


@socketio.on("stop_game")
def on_stop_game():
    global current_session
    with session_lock:
        if current_session:
            current_session.stop()
            current_session = None
    emit("game_stopped", {})
    logger.info("Game stopped by client.")


@socketio.on("set_volume")
def on_set_volume(data):
    vol = float(data.get("volume", 0.8))
    audio_manager.set_volume(vol)


@socketio.on("gesture")
def on_gesture(data):
    global current_session
    global client_clock_ms
    if current_session is None:
        return

    direction = data.get("direction", "")
    timestamp_ms = float(data.get("timestamp_ms", 0.0) or 0.0)
    client_clock_ms = max(client_clock_ms, timestamp_ms)
    current_session.push_external_gesture(direction, timestamp_ms)


@socketio.on("client_clock")
def on_client_clock(data):
    global client_clock_ms
    client_clock_ms = max(client_clock_ms, float(data.get("ms", 0.0) or 0.0))


# ------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------

def main():
    # Create directories if missing
    os.makedirs(MUSIC_DIR,   exist_ok=True)
    os.makedirs(BEATMAP_DIR, exist_ok=True)

    # Start hand tracker
    hand_tracker.start()
    logger.info("Hand tracker started.")

    port = int(os.environ.get("PORT", 5000))
    logger.info("Starting server on http://0.0.0.0:%d", port)
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    server = pywsgi.WSGIServer(
        ("0.0.0.0", port),
        app,
        handler_class=WebSocketHandler,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
