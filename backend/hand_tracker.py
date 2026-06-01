"""
Hand Tracker Module
Detects hand landmarks via MediaPipe and classifies swipe gestures.
Runs in its own thread, pushes gestures to a thread-safe queue.
"""

import cv2
import mediapipe as mp
import threading
import time
import queue
import numpy as np
import logging
import os

logger = logging.getLogger(__name__)

# Gesture directions
DIRECTION_LEFT  = "left"
DIRECTION_RIGHT = "right"
DIRECTION_UP    = "up"
DIRECTION_DOWN  = "down"

class HandTracker:
    def __init__(
        self,
        camera_index: int = 0,
        width: int = 320,
        height: int = 240,
        swipe_threshold: float = 0.12,   # normalised distance in one frame
        debounce_ms: int = 350,
        max_hands: int = 2,
    ):
        self.camera_index   = camera_index
        self.width          = width
        self.height         = height
        self.swipe_threshold = swipe_threshold
        self.debounce_ms    = debounce_ms

        self.gesture_queue: queue.Queue = queue.Queue(maxsize=64)

        self._running        = False
        self._thread: threading.Thread | None = None
        self._lock           = threading.Lock()

        # Track last gesture timestamp per direction for debounce
        self._last_gesture_time: dict[str, float] = {}

        # Track previous wrist positions per hand (keyed by hand index)
        self._prev_positions: dict[int, tuple[float, float]] = {}
        self._prev_time: float = 0.0

        # MediaPipe setup
        self._mp_hands = mp.solutions.hands
        self._hands = self._mp_hands.Hands(
            static_image_mode=False,
            max_num_hands=max_hands,
            min_detection_confidence=0.6,
            min_tracking_confidence=0.5,
        )

        # Camera preview toggle (shows annotated webcam window when True)
        self.show_preview = False

        # Initialise _latest_jpeg to a placeholder so get_frame_jpeg()
        # never returns None even before the camera thread starts.
        self._latest_jpeg: bytes | None = self._make_placeholder("No Camera")

        self._camera_available = self._probe_camera()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start the background capture/tracking thread."""
        if self._running:
            return
        if not self._camera_available:
            logger.info("Camera not available, using placeholder frame only.")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="HandTracker")
        self._thread.start()
        logger.info("HandTracker started.")

    def stop(self):
        """Stop the background thread gracefully."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        self._hands.close()
        logger.info("HandTracker stopped.")

    def get_gesture(self, block: bool = False, timeout: float = 0.05):
        """
        Pop the next gesture from the queue.
        Returns a dict like:
          { "direction": "left", "timestamp_ms": 1234567890.0, "confidence": 0.87 }
        Returns None if no gesture is available.
        """
        try:
            return self.gesture_queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def push_gesture(self, direction: str, timestamp_ms: float | None = None):
        """Accept a gesture from an external source (browser camera)."""
        timestamp_ms = timestamp_ms if timestamp_ms is not None else time.time() * 1000
        direction = str(direction).lower()
        if direction not in {DIRECTION_LEFT, DIRECTION_RIGHT, DIRECTION_UP, DIRECTION_DOWN}:
            return

        event = {"direction": direction, "timestamp_ms": timestamp_ms}
        try:
            self.gesture_queue.put_nowait(event)
        except queue.Full:
            pass

    def get_frame_jpeg(self) -> bytes | None:
        """Return the latest annotated frame as JPEG bytes (for live preview stream)."""
        with self._lock:
            return self._latest_jpeg

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _probe_camera(self) -> bool:
        """Return True only if the expected camera device exists."""
        if os.environ.get("ENABLE_CAMERA", "").lower() in {"1", "true", "yes"}:
            return True

        device_path = os.environ.get("CAMERA_DEVICE", f"/dev/video{self.camera_index}")
        return os.path.exists(device_path)

    def _run(self):
        if not self._camera_available:
            return

        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            logger.warning(
                "Camera %d not available — hand tracking disabled. "
                "On Linux, pass --device /dev/video0 to the container.",
                self.camera_index,
            )
            # Keep thread alive serving placeholder frames so /video_feed
            # doesn't stall the browser, but mark tracker as no-camera.
            self._latest_jpeg = self._make_placeholder("No Camera Available")
            while self._running:
                time.sleep(1)
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, 30)

        self._latest_jpeg = None
        self._prev_time = time.time()

        mp_draw = mp.solutions.drawing_utils

        while self._running:
            ret, frame = cap.read()
            if not ret:
                logger.warning("Camera read failed, retrying…")
                time.sleep(0.1)
                continue

            now = time.time()
            dt  = now - self._prev_time
            if dt <= 0:
                dt = 1e-6

            # Flip horizontally for mirror effect
            frame = cv2.flip(frame, 1)
            rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = self._hands.process(rgb)

            annotated = frame.copy()

            if results.multi_hand_landmarks:
                for idx, hand_landmarks in enumerate(results.multi_hand_landmarks):
                    # Draw skeleton
                    mp_draw.draw_landmarks(
                        annotated,
                        hand_landmarks,
                        self._mp_hands.HAND_CONNECTIONS,
                    )

                    # Wrist landmark (index 0)
                    wrist = hand_landmarks.landmark[0]
                    cx, cy = wrist.x, wrist.y  # normalised [0,1]

                    prev = self._prev_positions.get(idx)
                    if prev is not None:
                        dx = cx - prev[0]
                        dy = cy - prev[1]

                        # Scale dx/dy by frame rate to get velocity per second
                        vx = dx / dt
                        vy = dy / dt

                        direction = self._classify_swipe(vx, vy)
                        if direction:
                            self._emit_gesture(direction, now)

                    self._prev_positions[idx] = (cx, cy)

            # Clean up stale hand positions
            if not results.multi_hand_landmarks:
                self._prev_positions.clear()

            self._prev_time = now

            # Visual: draw gesture indicators on annotated frame
            self._draw_hud(annotated)

            # Encode frame to JPEG for browser preview
            _, jpeg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 70])
            with self._lock:
                self._latest_jpeg = jpeg.tobytes()

            if self.show_preview:
                cv2.imshow("Hand Tracker", annotated)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

        cap.release()
        if self.show_preview:
            cv2.destroyAllWindows()

    def _classify_swipe(self, vx: float, vy: float) -> str | None:
        """
        Returns direction if velocity exceeds threshold.
        Dominant axis wins; ratio check avoids diagonal false positives.
        """
        abs_vx, abs_vy = abs(vx), abs(vy)
        threshold = self.swipe_threshold * 30  # scale from normalised/frame → /second

        if abs_vx < threshold and abs_vy < threshold:
            return None

        if abs_vx >= abs_vy:
            # Horizontal dominant
            if abs_vx / (abs_vy + 1e-6) < 1.3:
                return None  # too diagonal
            return DIRECTION_RIGHT if vx > 0 else DIRECTION_LEFT
        else:
            # Vertical dominant
            if abs_vy / (abs_vx + 1e-6) < 1.3:
                return None
            # In image coords Y increases downward
            return DIRECTION_DOWN if vy > 0 else DIRECTION_UP

    def _emit_gesture(self, direction: str, now: float):
        """Push gesture to queue with debounce."""
        last = self._last_gesture_time.get(direction, 0.0)
        if (now - last) * 1000 < self.debounce_ms:
            return

        self._last_gesture_time[direction] = now
        event = {
            "direction":    direction,
            "timestamp_ms": now * 1000,
        }
        try:
            self.gesture_queue.put_nowait(event)
            logger.debug("Gesture: %s at %.0f ms", direction, event["timestamp_ms"])
        except queue.Full:
            pass  # drop if consumer is slow

    def _draw_hud(self, frame):
        """Draw directional arrows and last-gesture indicator on the frame."""
        h, w = frame.shape[:2]
        arrows = {
            DIRECTION_LEFT:  (20,      h // 2),
            DIRECTION_RIGHT: (w - 20,  h // 2),
            DIRECTION_UP:    (w // 2,  20),
            DIRECTION_DOWN:  (w // 2,  h - 20),
        }
        now = time.time() * 1000
        for direction, (px, py) in arrows.items():
            last = self._last_gesture_time.get(direction, 0)
            age  = now - last
            color = (0, 255, 0) if age < 300 else (80, 80, 80)
            cv2.circle(frame, (px, py), 12, color, -1)

    def _make_placeholder(self, text: str = "No Camera") -> bytes:
        """Return a JPEG placeholder frame shown when no camera is available."""
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        img[:] = (30, 30, 30)   # dark grey background
        cv2.putText(
            img, text,
            (int(320 / 2) - len(text) * 5, 120),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120, 120, 120), 1, cv2.LINE_AA,
        )
        _, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return jpeg.tobytes()
