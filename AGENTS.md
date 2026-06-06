# AGENTS.md

## Project: HandDance — Rhythm game using body-pose gestures via webcam

Monolithic Python Flask-SocketIO server (gevent async mode) serving plain HTML/CSS/JS frontend. No tests, no CI, no pre-commit, no lint/typecheck.

## Run

```bash
docker compose up --build          # http://localhost:5001 (5001→5000)
docker compose -f docker-compose.yml -f docker-compose.linux.yml up  # adds /dev/video0 + /dev/snd on Linux
```

Without Docker (macOS/Windows dev): `pip install -r requirements.txt && python backend/main.py`

## Architecture

- **`backend/main.py`** — entrypoint. Flask + SocketIO (gevent pywsgi). Multi-session via `sessions: dict[str, GameSession]` keyed by SocketIO SID. Routes: `/` (UI), `/api/songs`, `/api/beatmaps`, `/video_feed` (MJPEG on-demand stream), `/api/download` (yt-dlp), `/music/<path>` (serves audio to browser). Has `flask-limiter` + YouTube domain allowlist for `/api/download`.
- **`backend/game_logic.py`** — `GameSession` runs a 60 Hz thread: spawn notes, drain **per-session** gesture queue, judge timing, auto-miss. Each session has its own `queue.Queue` — no shared gesture state. Timing windows: PERFECT ±55ms, GOOD ±110ms, MISS 200ms. Scroll 360px/s, judgment line Y=580 of 680px canvas.
- **`backend/hand_tracker.py`** — MediaPipe Hands in own thread. Swipe threshold 0.12 norm. Debounce 350ms. Outputs MJPEG frames for `/video_feed` **only when at least one HTTP client is subscribed** (subscriber pattern). **Not the primary gesture source;** browser sends gestures via SocketIO.
- **`backend/audio_manager.py`** — pygame mixer with wall-clock fallback. Non-fatal init failure (`_pygame_ok=False`). Used for server-side timing sync only; actual playback is browser-side.
- **`backend/beatmap_parser.py`** — JSON beatmap loader. Auto-generator uses `random.seed(42)` for reproducible patterns.
- **`frontend/`** — 3 static files, no build step. Camera via `getUserMedia`.

## Gesture flow (critical)

1. Browser gets camera via `getUserMedia`, feeds it to **MediaPipe Pose** (`@mediapipe/pose` CDN).
2. `classifyPoseHold()` in `script.js:304` checks wrist vs. nose/shoulder/hip positions (threshold 0.10 norm).
3. Detected gestures are sent to server via SocketIO `gesture` event with `{direction, timestamp_ms}`.
4. `main.py:392` calls `sessions[sid].push_external_gesture()`, which enqueues into **that session's own queue** (not shared).
5. Game loop drains the per-session queue via `session._gesture_queue.get(block=False)`.
6. Browser also sends `client_clock` events every ~200ms (`performance.now() - browserClockBase`) for timing sync.

## Audio flow

Audio plays in the browser via `<audio>` element (`playBrowserAudio()` → `/music/<file>` route). Backend audio_manager provides the game's timing clock; if pygame fails, the game still runs on browser clock only.

## Key quirks

- `gevent monkey.patch_all()` must be **the very first import** in `main.py` (line 30-31), before any stdlib/Flask imports.
- Docker port **5001** on host → 5000 container. README says 5000; the compose file uses 5001.
- Frontend is **not** volume-mounted. Only `music/` and `beatmaps/` are mounted.
- Beatmap JSON must be named `<audio_stem>.json` in `beatmaps/`. `load_for_audio()` also scans all beatmaps for matching `audio_file`.
- yt-dlp downloads auto-generate a beatmap via `generate_auto_beatmap()` (seeded 42).
- Audio device failure is non-fatal; browser plays audio regardless.
- `_resolve_audio_filename()` handles URL-encoded, basename-only, and casefolded lookups.
- `SDL_AUDIODRIVER=alsa` and `PYGAME_HIDE_SUPPORT_PROMPT=1` set in Docker.
- `SECRET_KEY` auto-generates from `secrets.token_hex(32)` if not set via env.
- `CORS_ORIGIN` env var restricts allowed origins (default `http://localhost:5001`). Comma-separated for multiple.
- `/api/download` is rate-limited (5/min per IP) and URL-validated against YouTube domains only. `MAX_MUSIC_DIR_MB` env var (default 500) guards disk usage.
- `/video_feed` only encodes MJPEG frames while at least one HTTP client is subscribed (on-demand), saving CPU/bandwidth when idle.

## Layout

```
backend/     — Python server (5 modules)
frontend/    — HTML + CSS + JS (3 files, no build)
music/       — audio files (volume mount)
beatmaps/    — beatmap JSONs (volume mount)
```
