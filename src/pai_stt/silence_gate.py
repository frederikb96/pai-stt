"""Client-side silence gate.

After a sustained quiet stretch the gate stops letting audio through and the
caller tells the backend it is silent (`silence`). Capture continues: frames
keep arriving here, a one-second ring keeps the most recent ones, and when the
level rises again the ring is released first so the start of the next word is
not lost. The next audio frame sent after a withheld stretch carries its true
`sample_offset`, which is how the backend sees the gap.

Pure and synchronous: `push` takes a frame and returns what to send. Every
timer is in milliseconds of audio, never frame counts.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass, field
from typing import Optional

SAMPLE_RATE = 16000

QUIET_WINDOW_MS = 5000
PREROLL_MS = 1000
FLOOR_WINDOW_MS = 8000
FLOOR_PERCENTILE = 10
FLOOR_MIN_HISTORY_MS = 1000
AUTO_MARGIN_DB = 8.0
AUTO_OPEN_MIN_DB = -60.0
AUTO_MAX_FLOOR_DB = -50.0
HYSTERESIS_DB = 3.0
ONSET_MS = 150
LOUD_ONSET_DB = 10.0
SILENT_DB = -100.0


@dataclass(frozen=True)
class Frame:
    """One captured chunk: its take-relative sample offset and PCM16 bytes."""

    offset: int
    pcm: bytes

    @property
    def samples(self) -> int:
        return len(self.pcm) // 2

    @property
    def end(self) -> int:
        return self.offset + self.samples

    @property
    def duration_ms(self) -> float:
        return self.samples * 1000 / SAMPLE_RATE


@dataclass
class GateResult:
    """What to do with the frame just pushed.

    `send` is every frame to put on the wire, in order. `silence_at`, when
    set, is the `at_sample` for a `silence` frame to send after them.
    """

    send: list[Frame] = field(default_factory=list)
    silence_at: Optional[int] = None


def level_db(pcm: bytes) -> float:
    """RMS level of PCM16 little-endian samples in dBFS, clamped at SILENT_DB."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return SILENT_DB
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return 20 * math.log10(max(rms / 32768, 1e-5))


class SilenceGate:
    """Decides, per frame, whether audio is sent or withheld."""

    def __init__(self, enabled: bool, mode: str, manual_threshold_db: float) -> None:
        if mode not in ("auto", "manual"):
            raise ValueError(f"silence gate mode must be auto or manual, got {mode!r}")
        self._enabled = enabled
        self._auto = mode == "auto"
        self._manual_db = manual_threshold_db
        self.reset()

    def reset(self) -> None:
        """Forget all history: take start, `ready`, input device change."""
        self._withholding = False
        self._quiet_ms = 0.0
        self._above_run_ms = 0.0
        self._history: list[tuple[float, float]] = []  # (level_db, duration_ms)
        self._ring: list[tuple[Frame, float]] = []  # (frame, level_db)

    @property
    def is_withholding(self) -> bool:
        return self._withholding

    def _floor(self) -> tuple[float, float]:
        """Duration-weighted low percentile of recent levels, and the history length."""
        total = sum(d for _, d in self._history)
        target = total * FLOOR_PERCENTILE / 100
        acc = 0.0
        for lvl, dur in sorted(self._history):
            acc += dur
            if acc >= target:
                return lvl, total
        return SILENT_DB, total

    def _thresholds(self, floor: float) -> tuple[float, float]:
        opened = max(floor + AUTO_MARGIN_DB, AUTO_OPEN_MIN_DB) if self._auto else self._manual_db
        return opened, opened - HYSTERESIS_DB

    def push(self, frame: Frame, allowed: bool) -> GateResult:
        """Account for one captured frame. `allowed` is the backend's `silence_allowed`."""
        lvl = level_db(frame.pcm)
        dur = frame.duration_ms

        self._history.append((lvl, dur))
        total = sum(d for _, d in self._history)
        while len(self._history) > 1 and total - self._history[0][1] >= FLOOR_WINDOW_MS:
            total -= self._history.pop(0)[1]
        floor, history_ms = self._floor()
        opened, closed = self._thresholds(floor)
        floor_loud = self._auto and floor > AUTO_MAX_FLOOR_DB

        if not self._withholding:
            # In auto mode a frame must also sit below the loud-room ceiling to count as
            # quiet: at the start of a take the floor IS the speech level, so the close
            # threshold sits above it and the opening sentence would otherwise count.
            quiet = lvl < closed and (not self._auto or lvl < AUTO_MAX_FLOOR_DB)
            self._quiet_ms = self._quiet_ms + dur if quiet else 0.0
            floor_trusted = (not self._auto) or (
                history_ms >= FLOOR_MIN_HISTORY_MS and not floor_loud
            )
            if self._enabled and allowed and self._quiet_ms >= QUIET_WINDOW_MS and floor_trusted:
                self._withholding = True
                self._above_run_ms = 0.0
                self._ring = []
                return GateResult(send=[frame], silence_at=frame.end)
            return GateResult(send=[frame])

        self._ring.append((frame, lvl))
        ring_ms = 0.0
        keep_from = len(self._ring)
        while keep_from > 0 and ring_ms < PREROLL_MS:
            keep_from -= 1
            ring_ms += self._ring[keep_from][0].duration_ms
        self._ring = self._ring[keep_from:]

        self._above_run_ms = self._above_run_ms + dur if lvl >= opened else 0.0
        resume = (
            self._above_run_ms >= ONSET_MS
            or lvl >= opened + LOUD_ONSET_DB
            or not allowed
            or not self._enabled
            or floor_loud
        )
        if not resume:
            return GateResult()
        return GateResult(send=self._release())

    def _release(self) -> list[Frame]:
        frames = [f for f, _ in self._ring]
        self._ring = []
        self._withholding = False
        self._quiet_ms = 0.0
        self._above_run_ms = 0.0
        return frames

    def stop_flush(self) -> list[Frame]:
        """Frames to send at stop. While withholding: the ring, if any of it was speech."""
        if not self._withholding:
            return []
        floor, _ = self._floor()
        opened, _ = self._thresholds(floor)
        speech = any(lvl >= opened for _, lvl in self._ring)
        return self._release() if speech else []
