# syntax=docker/dockerfile:1

FROM python:3.11-slim

# ── System packages ────────────────────────────────────────────────
# Install everything in one layer so shared libraries are always present.
RUN apt-get update && apt-get install -y --no-install-recommends \
      # OpenCV headless runtime
      libglib2.0-0 \
      libsm6 \
      libxext6 \
      libxrender1 \
      # Audio: SDL2 (pygame) + ALSA
      libsdl2-2.0-0 \
      libsdl2-mixer-2.0-0 \
      libasound2 \
      pulseaudio-utils \
      # ffmpeg + ffprobe — required by yt-dlp for audio extraction
      ffmpeg \
      # Node.js — yt-dlp JS runtime for YouTube format extraction
      nodejs \
      # Build tools for pip wheels that need compilation
      gcc \
      python3-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Upgrade yt-dlp to latest (mitigates known CVEs) ───────────────
RUN pip install --no-cache-dir --upgrade yt-dlp

# ── Application code ───────────────────────────────────────────────
COPY backend/  /app/backend/
COPY frontend/ /app/frontend/

# ── Non-root user ──────────────────────────────────────────────────
RUN useradd -m -u 1000 gameuser \
 && mkdir -p /app/backend/music /app/backend/beatmaps \
 && chown -R gameuser:gameuser /app

USER gameuser

# ── Runtime environment ────────────────────────────────────────────
ENV MUSIC_DIR=/app/backend/music \
    BEATMAP_DIR=/app/backend/beatmaps \
    CAMERA_INDEX=0 \
    SWIPE_THRESHOLD=0.12 \
    PORT=5000 \
    PYTHONUNBUFFERED=1 \
    SDL_AUDIODRIVER=alsa \
    PYGAME_HIDE_SUPPORT_PROMPT=1

EXPOSE 5000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/')" || exit 1

CMD ["python", "/app/backend/main.py"]
