"""Shared silence-gate vectors: 100 ms frames at 16 kHz, offsets in samples.

Auto mode derives its open threshold from the tracked noise floor, never
below -60 dBFS, so a quiet room (-75) opens at -60 and closes at -63.
"""

import unittest
from typing import Optional

from pai_stt.silence_gate import Frame, GateResult, SilenceGate

FRAME_SAMPLES = 1600


def tone(db: float) -> bytes:
    amplitude = int(32768 * 10 ** (db / 20))
    return amplitude.to_bytes(2, "little", signed=True) * FRAME_SAMPLES


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
    """Speech for 3 s, then room quiet until the gate starts withholding."""
    run = auto()
    run.push(-40, 30)
    while not run.gate.is_withholding:
        run.push(-75)
        assert run.index < 200, "gate never withheld"
    return run


class TestGateVectors(unittest.TestCase):
    def test_v1_withholds_only_after_five_quiet_seconds_and_sends_the_completing_frame(
        self,
    ) -> None:
        run = withholding_run()
        # 3 s of speech plus 5 s of quiet, and the floor needs a moment to fall
        # from speech level before quiet counts at all.
        self.assertGreater(run.index, 80)
        self.assertLess(run.index, 90)
        last = run.results[-1]
        self.assertEqual(run.silence_points(), [run.index * FRAME_SAMPLES])
        self.assertEqual([f.offset for f in last.send], [(run.index - 1) * FRAME_SAMPLES])
        self.assertEqual(run.push(-75, 5).send, [])

    def test_v2_resumes_after_onset_and_sends_the_last_second_first(self) -> None:
        run = withholding_run()
        run.push(-75, 40)
        before = run.index
        self.assertEqual(run.push(-55).send, [])
        resumed = run.push(-55)
        offsets = [f.offset for f in resumed.send]
        self.assertEqual(offsets[0], (before + 2 - 10) * FRAME_SAMPLES)
        self.assertEqual(offsets[-1], (before + 1) * FRAME_SAMPLES)
        self.assertEqual(len(offsets), 10)
        self.assertFalse(run.gate.is_withholding)

    def test_v3_a_single_moderate_blip_does_not_resume(self) -> None:
        run = withholding_run()
        self.assertEqual(run.push(-58).send, [])
        self.assertEqual(run.push(-75).send, [])
        self.assertTrue(run.gate.is_withholding)

    def test_v4_a_loud_frame_resumes_at_once(self) -> None:
        run = withholding_run()
        run.push(-75, 3)
        resumed = run.push(-45)
        self.assertEqual(len(resumed.send), 4)
        self.assertFalse(run.gate.is_withholding)

    def test_v5_manual_threshold(self) -> None:
        run = Run(SilenceGate(enabled=True, mode="manual", manual_threshold_db=-45))
        run.push(-50, 49)
        self.assertEqual(run.silence_points(), [])
        run.push(-50)
        self.assertEqual(run.silence_points(), [80000])
        self.assertEqual(run.push(-44).send, [])
        self.assertTrue(run.push(-44).send)

    def test_v6_a_noisy_room_never_withholds(self) -> None:
        run = auto()
        run.push(-45, 150)
        self.assertEqual(run.silence_points(), [])

    def test_v7_backend_disallowing_resumes_with_the_ring(self) -> None:
        run = withholding_run()
        run.push(-75, 3)
        resumed = run.push(-75, allowed=False)
        self.assertEqual(len(resumed.send), 4)
        self.assertFalse(run.gate.is_withholding)

    def test_disallowed_from_the_start_never_withholds(self) -> None:
        run = auto()
        run.push(-75, 100, allowed=False)
        self.assertEqual(run.silence_points(), [])

    def test_disabled_gate_never_withholds(self) -> None:
        run = Run(SilenceGate(enabled=False, mode="auto", manual_threshold_db=-45))
        run.push(-75, 100)
        self.assertEqual(run.silence_points(), [])

    def test_stop_while_withholding_flushes_the_ring_only_if_it_holds_speech(self) -> None:
        quiet = withholding_run()
        quiet.push(-75, 5)
        self.assertEqual(quiet.gate.stop_flush(), [])

        spoken = withholding_run()
        spoken.push(-75, 5)
        spoken.push(-58)
        self.assertTrue(spoken.gate.stop_flush())

    def test_reset_forgets_withholding(self) -> None:
        run = withholding_run()
        run.gate.reset()
        self.assertFalse(run.gate.is_withholding)
        self.assertEqual(run.gate.stop_flush(), [])


if __name__ == "__main__":
    unittest.main()
