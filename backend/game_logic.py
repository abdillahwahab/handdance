"""
Game Logic Module
Core game loop: note spawning, timing, gesture judgement, scoring.
Runs as a background thread; communicates with the SocketIO server
via a shared event queue.
"""

import threading
import time
import uuid
import logging
import copy
from dataclasses import dataclass, field
from typing import List, Optional, Callable

from beatmap_parser import Beatmap, Note
from audio_manager import AudioManager
from hand_tracker import HandTracker

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Timing windows (milliseconds)
# ------------------------------------------------------------------
PERFECT_WINDOW_MS = 55
GOOD_WINDOW_MS    = 110
MISS_WINDOW_MS    = 200   # note auto-misses after this much past ideal time

# Scroll parameters
SCROLL_SPEED_PX_PER_S = 360   # pixels per second (at 1 × speed)
GAME_HEIGHT_PX        = 680   # assumed frontend canvas height
JUDGMENT_LINE_PX      = 580   # Y position of the judgment bar (from top)

# How many ms before ideal time a note becomes "visible" on screen
# = time for note to travel from top to judgment line
SPAWN_LEAD_MS = (JUDGMENT_LINE_PX / SCROLL_SPEED_PX_PER_S) * 1000  # ≈1611ms


# ------------------------------------------------------------------
# Score ratings
# ------------------------------------------------------------------
@dataclass
class HitResult:
    note_id:   str
    direction: str
    rating:    str    # "perfect" | "good" | "miss" | "wrong"
    points:    int
    diff_ms:   float  # signed timing error

    def to_dict(self) -> dict:
        return {
            "note_id":   self.note_id,
            "direction": self.direction,
            "rating":    self.rating,
            "points":    self.points,
            "diff_ms":   round(self.diff_ms, 1),
        }


@dataclass
class GameState:
    score:     int  = 0
    combo:     int  = 0
    max_combo: int  = 0
    perfects:  int  = 0
    goods:     int  = 0
    misses:    int  = 0
    total_notes: int = 0

    def to_dict(self) -> dict:
        return {
            "score":       self.score,
            "combo":       self.combo,
            "max_combo":   self.max_combo,
            "perfects":    self.perfects,
            "goods":       self.goods,
            "misses":      self.misses,
            "total_notes": self.total_notes,
            "accuracy":    self._accuracy(),
        }

    def _accuracy(self) -> float:
        judged = self.perfects + self.goods + self.misses
        if judged == 0:
            return 100.0
        return round((self.perfects * 2 + self.goods) / (judged * 2) * 100, 1)


class GameSession:
    """
    Manages a single game session (one song play-through).
    Spawns a game loop thread that:
      1. Reads current audio time.
      2. Activates upcoming notes.
      3. Polls hand-gesture queue.
      4. Judges gestures against active notes.
      5. Auto-misses expired notes.
      6. Fires callbacks so main.py can push SocketIO events.
    """

    def __init__(
        self,
        beatmap:       Beatmap,
        audio_manager: AudioManager,
        hand_tracker:  HandTracker,
        on_note_spawn:  Callable[[dict], None],
        on_hit_result:  Callable[[dict, dict], None],  # (hit_result, game_state)
        on_game_end:    Callable[[dict], None],
        session_id:    str = "",
    ):
        self.beatmap       = beatmap
        self.audio         = audio_manager
        self.hand          = hand_tracker
        self.session_id    = session_id or str(uuid.uuid4())[:8]

        self._on_note_spawn  = on_note_spawn
        self._on_hit_result  = on_hit_result
        self._on_game_end    = on_game_end

        self.state          = GameState(total_notes=len(beatmap.notes))

        # Deep-copy notes so original beatmap is untouched
        self._notes: List[Note]  = [copy.deepcopy(n) for n in beatmap.notes]
        self._spawn_idx: int     = 0   # index into self._notes for next-to-spawn
        self._active_notes: List[Note] = []  # notes currently on screen

        self._running  = False
        self._thread: Optional[threading.Thread] = None
        self._lock     = threading.Lock()
        self._start_perf: float = 0.0
        self._start_audio_ms: float = 0.0
        self._external_clock: Optional[Callable[[], float]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self.audio.play()
        self._start_perf = time.perf_counter()
        self._start_audio_ms = self.audio.get_position_ms()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"GameLoop-{self.session_id}"
        )
        self._thread.start()
        logger.info("GameSession %s started.", self.session_id)

    def set_external_clock(self, clock: Callable[[], float]):
        """Override game time source. Clock must return ms from song start."""
        self._external_clock = clock

    def stop(self):
        self._running = False
        self.audio.stop()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("GameSession %s stopped.", self.session_id)

    def get_active_notes(self) -> List[dict]:
        """Return serialisable list of active notes with current y_pos."""
        now_ms = self._now_ms()
        with self._lock:
            result = []
            for note in self._active_notes:
                # y_pos: 0 = top of screen, JUDGMENT_LINE_PX = judgment bar
                # note arrives at judgment line exactly at note.time_ms
                time_until_ms = note.time_ms - now_ms
                y_px = JUDGMENT_LINE_PX - (time_until_ms / 1000) * SCROLL_SPEED_PX_PER_S
                result.append({**note.to_dict(), "y_px": round(y_px, 1)})
        return result

    def get_state(self) -> dict:
        return self.state.to_dict()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _loop(self):
        tick_interval = 1 / 60  # 60 Hz update rate

        while self._running:
            loop_start = time.perf_counter()
            now_ms     = self._now_ms()

            # 1. Spawn upcoming notes
            self._spawn_notes(now_ms)

            # 2. Process pending gestures
            self._process_gestures(now_ms)

            # 3. Auto-miss expired notes
            self._auto_miss(now_ms)

            # 4. Check song finished
            if self.audio.is_finished() and self._spawn_idx >= len(self._notes):
                # Give a small grace period for last notes
                if not self._active_notes:
                    self._finish()
                    break

            elapsed  = time.perf_counter() - loop_start
            sleep_t  = tick_interval - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _spawn_notes(self, now_ms: float):
        """Move notes into _active_notes when they should appear on screen."""
        with self._lock:
            while self._spawn_idx < len(self._notes):
                note = self._notes[self._spawn_idx]
                spawn_time = note.time_ms - SPAWN_LEAD_MS
                if now_ms >= spawn_time:
                    self._active_notes.append(note)
                    self._spawn_idx += 1
                    self._on_note_spawn(note.to_dict())
                else:
                    break  # notes are time-sorted

    def _process_gestures(self, now_ms: float):
        """Drain gesture queue and judge each gesture."""
        while True:
            gesture = self.hand.get_gesture(block=False)
            if gesture is None:
                break
            gesture_ms = gesture["timestamp_ms"]
            direction  = gesture["direction"]
            self._judge(direction, gesture_ms)

    def push_external_gesture(self, direction: str, timestamp_ms: float | None = None):
        """Accept a gesture directly from the browser/client path."""
        self.hand.push_gesture(direction, timestamp_ms)

    def _now_ms(self) -> float:
        if self._external_clock is not None:
            return max(0.0, float(self._external_clock()))
        return self.audio.get_position_ms()

    def _judge(self, direction: str, gesture_ms: float):
        """
        Find the closest un-hit note in the given direction within MISS_WINDOW_MS.
        Rate it Perfect / Good / Miss.
        """
        with self._lock:
            candidates = [
                n for n in self._active_notes
                if n.direction == direction and not n.hit and not n.missed
            ]

        if not candidates:
            # Gesture with no matching note — wrong gesture or empty
            result = HitResult(
                note_id   = "",
                direction = direction,
                rating    = "wrong",
                points    = 0,
                diff_ms   = 0.0,
            )
            self._on_hit_result(result.to_dict(), self.state.to_dict())
            return

        # Pick closest note by timing
        now_audio = self.audio.get_position_ms()
        best = min(candidates, key=lambda n: abs(n.time_ms - gesture_ms))
        diff_ms = gesture_ms - best.time_ms  # positive = late, negative = early

        abs_diff = abs(diff_ms)
        if abs_diff <= PERFECT_WINDOW_MS:
            rating, points = "perfect", 2
        elif abs_diff <= GOOD_WINDOW_MS:
            rating, points = "good", 1
        elif abs_diff <= MISS_WINDOW_MS:
            rating, points = "miss", 0
        else:
            # too far from any note
            result = HitResult(
                note_id=best.note_id, direction=direction,
                rating="wrong", points=0, diff_ms=diff_ms,
            )
            self._on_hit_result(result.to_dict(), self.state.to_dict())
            return

        with self._lock:
            best.hit = True
            try:
                self._active_notes.remove(best)
            except ValueError:
                pass

        # Update state
        self.state.score += points
        if rating in ("perfect", "good"):
            self.state.combo += 1
            self.state.max_combo = max(self.state.max_combo, self.state.combo)
        else:
            self.state.combo = 0

        if rating == "perfect":
            self.state.perfects += 1
        elif rating == "good":
            self.state.goods += 1
        else:
            self.state.misses += 1

        result = HitResult(
            note_id=best.note_id, direction=direction,
            rating=rating, points=points, diff_ms=diff_ms,
        )
        self._on_hit_result(result.to_dict(), self.state.to_dict())
        logger.debug("Judge %s → %s (diff %.0fms, +%d)", direction, rating, diff_ms, points)

    def _auto_miss(self, now_ms: float):
        """Mark notes that are past the miss window."""
        with self._lock:
            expired = [
                n for n in self._active_notes
                if not n.hit and not n.missed
                and (now_ms - n.time_ms) > MISS_WINDOW_MS
            ]

        for note in expired:
            with self._lock:
                note.missed = True
                try:
                    self._active_notes.remove(note)
                except ValueError:
                    pass

            self.state.combo = 0
            self.state.misses += 1

            result = HitResult(
                note_id=note.note_id, direction=note.direction,
                rating="miss", points=0, diff_ms=MISS_WINDOW_MS,
            )
            self._on_hit_result(result.to_dict(), self.state.to_dict())
            logger.debug("Auto-miss: %s", note.note_id)

    def _finish(self):
        self._running = False
        final_state = self.state.to_dict()
        logger.info("Game over. Score: %d | Accuracy: %.1f%%",
                    final_state["score"], final_state["accuracy"])
        self._on_game_end(final_state)
