"""
Audio Manager Module
Handles audio playback and provides an accurate current-time clock
that the game loop uses as master clock for note synchronisation.
"""

import threading
import time
import logging
import os

logger = logging.getLogger(__name__)


class AudioManager:
    """
    Wraps pygame.mixer for audio playback.
    Uses a software timer anchored to the moment play() is called so that
    get_position_ms() is reliable even on systems where pygame's built-in
    get_pos() drifts after long playback.
    """

    def __init__(self, music_dir: str = "/app/backend/music"):
        self.music_dir   = music_dir
        self._loaded     = False
        self._playing    = False
        self._paused     = False
        self._start_time: float = 0.0   # wall-clock time when play started
        self._pause_offset: float = 0.0 # accumulated paused duration
        self._duration_ms: float = 0.0
        self._current_file: str = ""
        self._lock = threading.Lock()

        self._pygame_ok = False
        self._init_pygame()

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def _init_pygame(self):
        try:
            import pygame
            if os.environ.get("SDL_AUDIODRIVER") == "alsa":
                # Skip probing if no ALSA device is present; use a silent fallback.
                if not self._has_alsa_device():
                    raise RuntimeError("No ALSA device available")
            pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=512)
            pygame.mixer.init()
            self._pygame = pygame
            self._pygame_ok = True
            logger.info("pygame.mixer initialised.")
        except Exception as exc:
            logger.warning("pygame.mixer init failed: %s — audio disabled.", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_songs(self) -> list[str]:
        """Return a list of supported audio filenames in music_dir."""
        supported = (".mp3", ".ogg", ".wav", ".flac")
        try:
            return sorted(
                f for f in os.listdir(self.music_dir)
                if f.lower().endswith(supported)
            )
        except FileNotFoundError:
            return []

    def load(self, filename: str) -> bool:
        """Load an audio file from music_dir. Returns True on success."""
        path = os.path.join(self.music_dir, filename)
        if not os.path.isfile(path):
            logger.error("Audio file not found: %s", path)
            return False

        # Always accept the file so the browser can play it even if the
        # container has no audio device (common on macOS / OrbStack).
        try:
            if self._pygame_ok:
                self._pygame.mixer.music.load(path)

            self._loaded       = True
            self._current_file = filename
            self._playing      = False
            self._paused       = False
            self._pause_offset = 0.0

            # Estimate duration using mutagen if available, else default
            self._duration_ms = self._get_duration_ms(path)
            logger.info("Loaded '%s' (%.1f s)", filename, self._duration_ms / 1000)
            return True
        except Exception as exc:
            logger.error("Failed to load audio: %s", exc)
            return False

    def play(self, start_ms: float = 0.0):
        """Start playback from start_ms."""
        if not self._loaded:
            return

        with self._lock:
            start_s = start_ms / 1000.0
            if self._pygame_ok:
                self._pygame.mixer.music.play(start=start_s)
            self._start_time   = time.perf_counter() - start_s
            self._pause_offset = 0.0
            self._playing      = True
            self._paused       = False
        logger.info("Audio playback started.")

    def pause(self):
        if not self._playing or self._paused:
            return
        with self._lock:
            if self._pygame_ok:
                self._pygame.mixer.music.pause()
            self._pause_start = time.perf_counter()
            self._paused      = True

    def resume(self):
        if not self._pygame_ok or not self._paused:
            return
        with self._lock:
            self._pause_offset += time.perf_counter() - self._pause_start
            if self._pygame_ok:
                self._pygame.mixer.music.unpause()
            self._paused = False

    def stop(self):
        with self._lock:
            if self._pygame_ok:
                self._pygame.mixer.music.stop()
            self._playing = False
            self._paused  = False
        logger.info("Audio stopped.")

    def get_position_ms(self) -> float:
        """
        Return current playback position in milliseconds.
        Uses a wall-clock timer for accuracy.
        """
        if not self._playing:
            return 0.0
        if self._paused:
            with self._lock:
                return (self._pause_start - self._start_time - self._pause_offset) * 1000
        elapsed = time.perf_counter() - self._start_time - self._pause_offset
        return max(0.0, elapsed * 1000)

    def get_duration_ms(self) -> float:
        return self._duration_ms

    def is_playing(self) -> bool:
        if not self._loaded:
            return False
        if not self._pygame_ok:
            return self._playing
        return self._pygame.mixer.music.get_busy() or self._playing

    def is_finished(self) -> bool:
        if not self._loaded:
            return False
        if not self._playing:
            return False
        if not self._pygame_ok:
            return self.get_position_ms() >= self._duration_ms > 0
        return not self._pygame.mixer.music.get_busy()

    def set_volume(self, volume: float):
        """Set volume 0.0 – 1.0."""
        if self._pygame_ok:
            self._pygame.mixer.music.set_volume(max(0.0, min(1.0, volume)))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_duration_ms(self, path: str) -> float:
        """Try mutagen first, fall back to 0."""
        try:
            from mutagen import File as MutagenFile
            af = MutagenFile(path)
            if af is not None and af.info:
                return af.info.length * 1000
        except Exception:
            pass
        return 0.0

    def _has_alsa_device(self) -> bool:
        """Best-effort check for an ALSA device in Linux containers."""
        return os.path.exists("/dev/snd")
