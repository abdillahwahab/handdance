"""
Main entry point — Flask-SocketIO server.

Routes:
  GET  /                → index.html
  GET  /static/<path>   → frontend static files
  GET  /api/songs       → list available audio files
  GET  /api/beatmaps    → list available beatmaps
  GET  /video_feed      → MJPEG stream from hand-tracker camera (on-demand)

SocketIO events (server → client):
  game_started     – beatmap metadata when game begins
  note_spawn       – { id, direction, time_ms }
  note_update      – list of active notes with current y_px (60 Hz)
  hit_result       – { note_id, direction, rating, points, diff_ms }
  score_update     – full GameState dict
  game_over        – final score summary
  error            – error message string

SocketIO events (client → server):
  start_game       – { youtube_url, difficulty, bpm }
  stop_game        – stop current session
"""

# gevent monkey-patch MUST happen before any other import so that
# stdlib sockets, threading, and ssl all become gevent-cooperative.
from gevent import monkey
monkey.patch_all()

import os
import sys
import logging
import secrets
import re
import threading
import time
from urllib.parse import unquote

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

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
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

cors_origin_str = os.environ.get("CORS_ORIGIN", "http://localhost:5001")
cors_allowed_origins = (
    [o.strip() for o in cors_origin_str.split(",")]
    if "," in cors_origin_str
    else cors_origin_str
)

socketio = SocketIO(
    app,
    cors_allowed_origins = cors_allowed_origins,
    async_mode          = "gevent",
    logger              = False,
    engineio_logger     = False,
)

# Rate limiter (in-memory, no external dep needed)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
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

# Multi-session support: each SocketIO SID maps to its own game session + clock
sessions: dict[str, GameSession] = {}
client_clocks: dict[str, float] = {}
client_clock_started: dict[str, float] = {}
ip_connections: dict[str, int] = {}

# Session limits
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "50"))
MAX_SESSIONS_PER_IP = int(os.environ.get("MAX_SESSIONS_PER_IP", "3"))
# ------------------------------------------------------------------
# YouTube video ID extraction
# ------------------------------------------------------------------
YT_URL_PATTERN = re.compile(
    r"(?:https?://)?"
    r"(?:www\.)?"
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)"
    r"([a-zA-Z0-9_-]{11})"
)

def _extract_youtube_video_id(url: str) -> str | None:
    m = YT_URL_PATTERN.search(url.strip())
    return m.group(1) if m else None

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
@limiter.limit("30 per minute")
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
@limiter.limit("60 per minute")
def music_file(filename):
    return send_from_directory(MUSIC_DIR, filename)

@app.route("/api/beatmaps")
@limiter.limit("30 per minute")
def api_beatmaps():
    return jsonify(beatmap_parser.list_beatmaps())

@app.route("/video_feed")
def video_feed():
    """MJPEG stream — only encodes frames while at least one client is connected."""
    client_id = hand_tracker.subscribe_feed()
    def generate():
        try:
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
        finally:
            hand_tracker.unsubscribe_feed(client_id)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )




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
    sid = request.sid
    client_ip = request.remote_addr or "unknown"
    current = ip_connections.get(client_ip, 0)
    if current >= MAX_SESSIONS_PER_IP:
        logger.warning("IP %s exceeds max connections (%d)", client_ip, MAX_SESSIONS_PER_IP)
        return False
    ip_connections[client_ip] = current + 1
    logger.info("Client connected: %s from %s (IP conns=%d, total=%d)",
                sid, client_ip, current + 1, len(sessions))
    client_clocks[sid] = 0.0
    client_clock_started[sid] = time.time() * 1000
    emit("song_list", _build_song_list())


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    client_ip = request.remote_addr or "unknown"
    if client_ip in ip_connections:
        ip_connections[client_ip] = max(0, ip_connections[client_ip] - 1)
        if ip_connections[client_ip] == 0:
            del ip_connections[client_ip]
    session = sessions.pop(sid, None)
    if session:
        session.stop()
    client_clocks.pop(sid, None)
    client_clock_started.pop(sid, None)
    logger.info("Client disconnected: %s", sid)


@socketio.on("start_game")
def on_start_game(data):
    sid = request.sid
    difficulty = data.get("difficulty", "medium")
    bpm_override = float(data.get("bpm", 0))

    if len(sessions) >= MAX_SESSIONS:
        emit("error", "Server is full. Try again later.")
        logger.warning("Session rejected: server full (%d)", MAX_SESSIONS)
        return

    existing = sessions.pop(sid, None)
    if existing:
        existing.stop()

    client_clock_started[sid] = time.time() * 1000
    client_clocks[sid] = 0.0

    video_id = None
    youtube_url = str(data.get("youtube_url", "")).strip()

    if youtube_url:
        video_id = _extract_youtube_video_id(youtube_url)
        if not video_id:
            emit("error", "Invalid YouTube URL")
            return
        bpm = bpm_override or 120
        duration_ms = 300_000
        beatmap = beatmap_parser.generate_auto_beatmap(
            audio_filename=f"youtube_{video_id}",
            bpm=bpm,
            duration_ms=duration_ms,
            difficulty=difficulty,
        )
        logger.info("YouTube game: video=%s BPM=%.0f", video_id, bpm)
    else:
        requested_audio = str(data.get("audio_file", ""))
        audio_file = _resolve_audio_filename(requested_audio)
        if not audio_file:
            emit("error", f"Cannot load audio file: {requested_audio}")
            return

        duration_ms = 180_000
        audio_path = os.path.join(MUSIC_DIR, audio_file)
        if os.path.isfile(audio_path):
            try:
                from mutagen import File as MutagenFile
                af = MutagenFile(audio_path)
                if af is not None and af.info:
                    duration_ms = af.info.length * 1000
            except Exception:
                pass

        beatmap = beatmap_parser.load_for_audio(audio_file)
        if beatmap is None:
            bpm = bpm_override or 120
            logger.info("No beatmap found for '%s', auto-generating (BPM=%.0f).", audio_file, bpm)
            beatmap = beatmap_parser.generate_auto_beatmap(
                audio_filename=audio_file,
                bpm=bpm,
                duration_ms=duration_ms,
                difficulty=difficulty,
            )
        else:
            last_note_ms = max(n.time_ms for n in beatmap.notes)
            duration_ms = max(duration_ms, last_note_ms + 2000)

    def on_note_spawn(note: dict):
        socketio.emit("note_spawn", note, to=sid)

    def on_hit_result(result: dict, state: dict):
        socketio.emit("hit_result", result, to=sid)
        socketio.emit("score_update", state, to=sid)

    def on_game_end(state: dict):
        socketio.emit("game_over", state, to=sid)

    session = GameSession(
        beatmap       = beatmap,
        audio_manager = audio_manager,
        duration_ms   = duration_ms,
        on_note_spawn = on_note_spawn,
        on_hit_result = on_hit_result,
        on_game_end   = on_game_end,
    )

    sessions[sid] = session
    session.set_external_clock(lambda: client_clocks.get(sid, 0.0))
    session.start()

    response = {
        "beatmap":   beatmap.to_dict(),
        "scroll_px": 360,
        "height_px": 680,
        "judgment_px": 580,
        "server_start_ms": client_clock_started.get(sid, 0.0),
    }
    if video_id:
        response["video_id"] = video_id

    emit("game_started", response)
    logger.info("Game started: %s (%s) [sid=%s]", video_id or audio_file, difficulty, sid)

    _start_note_push_thread(session, sid)


def _start_note_push_thread(session: GameSession, sid: str):
    """Push active note positions to the specific client at ~30 FPS."""
    def pusher():
        while session._running:
            notes = session.get_active_notes()
            socketio.emit("note_update", notes, to=sid)
            time.sleep(1 / 30)

    t = threading.Thread(target=pusher, daemon=True, name=f"NotePusher-{sid}")
    t.start()


@socketio.on("finish_game")
def on_finish_game():
    sid = request.sid
    session = sessions.get(sid)
    if session:
        session.finish()
    logger.info("Game finished by client (YouTube ended) [sid=%s ip=%s].", sid, request.remote_addr)


@socketio.on("stop_game")
def on_stop_game():
    sid = request.sid
    session = sessions.pop(sid, None)
    if session:
        session.stop()
    client_clocks.pop(sid, None)
    client_clock_started.pop(sid, None)
    emit("game_stopped", {})
    logger.info("Game stopped by client [sid=%s ip=%s].", sid, request.remote_addr)


@socketio.on("set_volume")
def on_set_volume(data):
    vol = float(data.get("volume", 0.8))
    audio_manager.set_volume(vol)


@socketio.on("gesture")
def on_gesture(data):
    sid = request.sid
    session = sessions.get(sid)
    if session is None:
        return

    direction = data.get("direction", "")
    timestamp_ms = float(data.get("timestamp_ms", 0.0) or 0.0)
    client_clocks[sid] = max(client_clocks.get(sid, 0.0), timestamp_ms)
    session.push_external_gesture(direction, timestamp_ms)


@socketio.on("client_clock")
def on_client_clock(data):
    sid = request.sid
    client_clocks[sid] = max(
        client_clocks.get(sid, 0.0), float(data.get("ms", 0.0) or 0.0)
    )


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
