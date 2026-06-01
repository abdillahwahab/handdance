"""
Beatmap Parser Module
Reads beatmap JSON files and exposes sorted note lists.

Beatmap JSON format:
{
    "title":      "Song Name",
    "artist":     "Artist Name",
    "audio_file": "song.mp3",
    "bpm":        128,
    "offset":     0.5,      // seconds before first note
    "difficulty": "medium", // easy | medium | hard
    "notes": [
        {"time": 1.0, "direction": "left"},
        {"time": 1.5, "direction": "right"},
        {"time": 2.0, "direction": "up"},
        {"time": 2.5, "direction": "down"}
    ]
}

A beatmap file must live in the beatmaps/ directory and be named
<audio_filename_without_extension>.json   (e.g. song.json for song.mp3)
or any .json file that the user selects.
"""

import json
import os
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

VALID_DIRECTIONS = {"left", "right", "up", "down"}


@dataclass
class Note:
    note_id: str         # unique id
    time_ms: float       # ideal hit time in milliseconds from audio start
    direction: str       # left | right | up | down
    hit: bool = False    # was it hit?
    missed: bool = False # was it missed (window expired)?

    def to_dict(self) -> dict:
        return {
            "id":        self.note_id,
            "time_ms":   self.time_ms,
            "direction": self.direction,
            "hit":       self.hit,
            "missed":    self.missed,
        }


@dataclass
class Beatmap:
    title:      str
    artist:     str
    audio_file: str
    bpm:        float
    offset_ms:  float          # offset in ms
    difficulty: str
    notes:      List[Note] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title":      self.title,
            "artist":     self.artist,
            "audio_file": self.audio_file,
            "bpm":        self.bpm,
            "offset_ms":  self.offset_ms,
            "difficulty": self.difficulty,
            "note_count": len(self.notes),
        }


class BeatmapParser:
    def __init__(self, beatmap_dir: str = "/app/backend/beatmaps"):
        self.beatmap_dir = beatmap_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_beatmaps(self) -> list[dict]:
        """
        Return minimal metadata for all .json files in beatmap_dir.
        Each item: { "filename": "...", "title": "...", "artist": "...",
                     "audio_file": "...", "bpm": ..., "difficulty": ... }
        """
        results = []
        try:
            files = [f for f in os.listdir(self.beatmap_dir) if f.endswith(".json")]
        except FileNotFoundError:
            return []

        for fname in sorted(files):
            try:
                bm = self.load(fname)
                meta = bm.to_dict()
                meta["filename"] = fname
                results.append(meta)
            except Exception as exc:
                logger.warning("Skipping beatmap '%s': %s", fname, exc)

        return results

    def load(self, filename: str) -> Beatmap:
        """Load and validate a beatmap JSON file. Raises ValueError on errors."""
        path = os.path.join(self.beatmap_dir, filename)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Beatmap file not found: {path}")

        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        return self._parse(data)

    def load_for_audio(self, audio_filename: str) -> Optional["Beatmap"]:
        """
        Try to find a beatmap matching the given audio filename.
        Looks for <stem>.json first, then any beatmap pointing to that audio file.
        """
        stem = os.path.splitext(audio_filename)[0]
        candidates = [stem + ".json"]

        # Also allow a sanitized variant for old YouTube titles.
        sanitized = stem.replace("/", " ").replace("⧸", " ").strip()
        if sanitized and sanitized + ".json" not in candidates:
            candidates.append(sanitized + ".json")

        for candidate in candidates:
            candidate_path = os.path.join(self.beatmap_dir, candidate)
            if os.path.isfile(candidate_path):
                try:
                    return self.load(candidate)
                except Exception as exc:
                    logger.warning("Could not load beatmap '%s': %s", candidate, exc)

        # Fallback: scan all beatmaps for matching audio_file
        for bm_meta in self.list_beatmaps():
            if bm_meta.get("audio_file") == audio_filename or os.path.basename(str(bm_meta.get("audio_file", ""))) == os.path.basename(audio_filename):
                try:
                    return self.load(bm_meta["filename"])
                except Exception:
                    pass

        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _parse(self, data: dict) -> Beatmap:
        required = ("audio_file", "notes")
        for key in required:
            if key not in data:
                raise ValueError(f"Beatmap missing required field: '{key}'")

        notes_raw = data["notes"]
        if not isinstance(notes_raw, list):
            raise ValueError("'notes' must be a list.")

        bpm      = float(data.get("bpm", 120))
        offset_s = float(data.get("offset", 0.0))

        notes: List[Note] = []
        for i, n in enumerate(notes_raw):
            if "time" not in n or "direction" not in n:
                raise ValueError(f"Note {i} missing 'time' or 'direction'.")
            direction = str(n["direction"]).lower()
            if direction not in VALID_DIRECTIONS:
                raise ValueError(f"Note {i} has invalid direction '{direction}'.")

            time_s  = float(n["time"])
            time_ms = (time_s + offset_s) * 1000

            notes.append(Note(
                note_id   = f"note_{i}_{direction}",
                time_ms   = time_ms,
                direction = direction,
            ))

        # Sort by time to ensure correct ordering
        notes.sort(key=lambda n: n.time_ms)

        return Beatmap(
            title      = str(data.get("title", "Unknown Title")),
            artist     = str(data.get("artist", "Unknown Artist")),
            audio_file = str(data["audio_file"]),
            bpm        = bpm,
            offset_ms  = offset_s * 1000,
            difficulty = str(data.get("difficulty", "medium")),
            notes      = notes,
        )

    def generate_auto_beatmap(
        self,
        audio_filename: str,
        bpm: float = 120,
        duration_ms: float = 60_000,
        difficulty: str = "medium",
    ) -> Beatmap:
        """
        Auto-generate a simple beatmap for songs that have no .json file.
        Places notes every beat (easy), half-beat (medium), or quarter-beat (hard).
        """
        import random

        divisions = {"easy": 1, "medium": 2, "hard": 4}.get(difficulty, 2)
        beat_ms   = 60_000 / bpm
        step_ms   = beat_ms / divisions

        directions = ["left", "right", "up", "down"]
        random.seed(42)  # reproducible pattern

        notes = []
        t_ms  = beat_ms * 2  # Start 2 beats in to give player time to react
        idx   = 0
        while t_ms < duration_ms - beat_ms * 2:
            direction = directions[idx % 4]
            idx += 1
            notes.append(Note(
                note_id   = f"auto_{idx}_{direction}",
                time_ms   = t_ms,
                direction = direction,
            ))
            t_ms += step_ms

        return Beatmap(
            title      = os.path.splitext(audio_filename)[0],
            artist     = "Auto-generated",
            audio_file = audio_filename,
            bpm        = bpm,
            offset_ms  = 0.0,
            difficulty = difficulty,
            notes      = notes,
        )
