"""Shared silence-gate vectors: 100 ms frames at 16 kHz, offsets in samples.

The same vector table runs in the iOS and web clients. Frames alternate +A/-A
with A = round(32768 * 10^(dB/20)). Auto mode derives its open threshold from
the tracked noise floor, never below -60 dBFS, so a quiet room (-75) opens at
-60 and closes at -63; a frame only counts as quiet there if it is also below
-50 dBFS.
"""

import unittest
from typing import Optional

from pai_stt.silence_gate import Frame, GateResult, SilenceGate

FRAME_SAMPLES = 1600


def tone(db: float) -> bytes:
    amplitude = round(32768 * 10 ** (db / 20))
    pair = amplitude.to_bytes(2, "little", signed=True) + (-amplitude).to_bytes(
        2, "little", signed=True
    )
    return pair * (FRAME_SAMPLES // 2)


class Run:
    """Pushes constant-level frames and keeps what the gate said about each."""

    def __init__(self, gate: SilenceGate) -> None:
        self.gate = gate
        self.index = 0
        self.results: list[GateResult] = []

    def push(self, db: float, count: int = 1, allowed: bool = True) -> GateResult:
        result = GateResult()
        for _ in range(count):
            frame = Frame(self.index * FRAME_SAMPLES, tone(db))
            self.index += 1
            result = self.gate.push(frame, allowed)
            self.results.append(result)
        return result

    def silence_points(self) -> list[Optional[int]]:
        return [r.silence_at for r in self.results if r.silence_at is not None]


def auto() -> Run:
    return Run(SilenceGate(enabled=True, mode="auto", manual_threshold_db=-45))


def withholding_run() -> Run:
    """V1's input: 3 s of speech, then 6 s of room quiet (withholds at 128000)."""
    run = auto()
    run.push(-40, 30)
    run.push(-75, 60)
    return run


class TestGateVectors(unittest.TestCase):
    def test_v1_withholds_after_five_quiet_seconds_and_sends_the_completing_frame(self) -> None:
        run = auto()
        run.push(-40, 30)
        run.push(-75, 30)
        self.assertFalse(run.gate.is_withholding)
        self.assertEqual(run.silence_points(), [])
        run.push(-75, 30)
        self.assertEqual(run.silence_points(), [128000])
        self.assertEqual(sum(len(r.send) for r in run.results), 80)
        self.assertEqual(max(f.end for r in run.results for f in r.send), 128000)
        self.assertTrue(run.gate.is_withholding)

    def test_v2_resumes_on_a_loud_onset_and_sends_the_last_second_first(self) -> None:
        run = auto()
        run.push(-40, 30)
        run.push(-75, 90)
        resumed = run.push(-40)
        self.assertEqual(len(resumed.send), 10)
        self.assertEqual(resumed.send[0].offset, 177600)
        self.assertEqual(resumed.send[-1].end, 193600)
        self.assertTrue(all(f.offset >= 128000 for f in resumed.send))
        self.assertFalse(run.gate.is_withholding)
        live = run.push(-40)
        self.assertEqual([f.offset for f in live.send], [193600])

    def test_v3_a_single_moderate_blip_does_not_resume(self) -> None:
        run = auto()
        run.push(-40, 30)
        run.push(-75, 60)
        self.assertTrue(run.gate.is_withholding)
        sent = [f for r in (run.push(-56), run.push(-75, 5)) for f in r.send]
        self.assertEqual(sent, [])
        self.assertTrue(run.gate.is_withholding)

    def test_v4_a_loud_frame_resumes_at_once(self) -> None:
        run = auto()
        run.push(-40, 30)
        run.push(-75, 60)
        self.assertTrue(run.gate.is_withholding)
        run.push(-48)
        self.assertFalse(run.gate.is_withholding)

    def test_v5_manual_threshold(self) -> None:
        run = Run(SilenceGate(enabled=True, mode="manual", manual_threshold_db=-45))
        run.push(-50, 60)
        self.assertEqual(run.silence_points(), [80000])
        self.assertEqual(run.push(-44).send, [])
        self.assertTrue(run.gate.is_withholding)
        self.assertTrue(run.push(-44).send)
        self.assertFalse(run.gate.is_withholding)

    def test_v6_a_noisy_room_never_withholds(self) -> None:
        run = auto()
        run.push(-45, 150)
        self.assertEqual(run.silence_points(), [])
        self.assertEqual(sum(len(r.send) for r in run.results), 150)

    def test_v7_backend_disallowing_resumes_with_the_ring(self) -> None:
        # The gate has no separate allowed setter: the next frame carries the flag. That
        # frame joins the ring, so the ring ends one frame later than the iOS vector's.
        run = auto()
        run.push(-40, 30)
        run.push(-75, 70)
        resumed = run.push(-75, allowed=False)
        self.assertEqual(len(resumed.send), 10)
        self.assertEqual(resumed.send[-1].end, 101 * FRAME_SAMPLES)
        self.assertFalse(run.gate.is_withholding)

    def test_stop_with_a_quiet_ring_flushes_nothing(self) -> None:
        run = auto()
        run.push(-40, 30)
        run.push(-75, 60)
        self.assertEqual(run.gate.stop_flush(), [])

    def test_stop_flushes_the_ring_when_it_holds_speech(self) -> None:
        run = auto()
        run.push(-40, 30)
        run.push(-75, 65)
        run.push(-58)
        self.assertTrue(run.gate.stop_flush())

    def test_disallowed_from_the_start_never_withholds(self) -> None:
        run = auto()
        run.push(-75, 100, allowed=False)
        self.assertEqual(run.silence_points(), [])

    def test_disabled_gate_never_withholds(self) -> None:
        run = Run(SilenceGate(enabled=False, mode="auto", manual_threshold_db=-45))
        run.push(-40, 30)
        run.push(-100, 100)
        self.assertEqual(run.silence_points(), [])

    def test_reset_forgets_withholding(self) -> None:
        run = withholding_run()
        run.gate.reset()
        self.assertFalse(run.gate.is_withholding)
        self.assertEqual(run.gate.stop_flush(), [])


if __name__ == "__main__":
    unittest.main()
