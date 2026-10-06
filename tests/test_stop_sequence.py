"""Stop sequence: tail, drain to EOF, close the gate, wait for the receipt, then bye."""

import asyncio
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Optional

from pai_stt import daemon as daemon_module
from pai_stt import recordings
from pai_stt.daemon import CHUNK_BYTES, PaiSttDaemon, State
from pai_stt.silence_gate import SilenceGate
from pai_stt.voice_client import TakeDone


class FakePwRecord:
    """A pw-record whose pipe still holds audio when it is told to stop."""

    def __init__(self, pending: bytes) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(pending)
        self.pid = 1

    def terminate(self) -> None:
        self.stdout.feed_eof()

    def kill(self) -> None:
        self.stdout.feed_eof()

    async def wait(self) -> int:
        return 0


class FakeVoice:
    def __init__(self, ended: Optional[str]) -> None:
        self.calls: list[str] = []
        self.offsets: list[int] = []
        self.silence_allowed = False
        self._ended = ended

    async def send_audio(self, offset: int, pcm: bytes) -> None:
        self.calls.append("audio")
        self.offsets.append(offset)

    async def send_silence(self, at_sample: int) -> None:
        self.calls.append("silence")

    async def close_gate(self, reason: str) -> None:
        self.calls.append("close_gate")

    async def wait_take_done(self, take_id: str, timeout: float) -> Optional[TakeDone]:
        self.calls.append("wait_take_done")
        return TakeDone(take_id, 0, self._ended) if self._ended else None

    async def bye(self, reason: str) -> None:
        self.calls.append("bye")

    async def close(self) -> None:
        self.calls.append("close")


async def _stop(
    ended: Optional[str],
    directory: Path,
    chunks: int = 5,
    batch: Optional[str] = "batch text",
    copied: Optional[list[str]] = None,
    tail_start: Optional[int] = None,
    backfill_ok: Optional[bool] = None,
    requests: Optional[list[tuple[int, str]]] = None,
) -> tuple[PaiSttDaemon, FakeVoice]:
    """Stop a recording; `batch` is what re-transcribing returns, None for a failure.

    `tail_start` stops with the socket still down; `backfill_ok` adds a gap backfill
    that finished that way. `requests` collects each batch call's audio length and
    previous text.
    """
    daemon = PaiSttDaemon(
        {"transcription_timeout": 5, "pai_cloud": {"socket_url": "wss://example/socket"}}
    )
    voice = FakeVoice(ended)
    daemon.voice = voice  # type: ignore[assignment]
    daemon.pw_record_proc = FakePwRecord(bytes(CHUNK_BYTES * chunks))  # type: ignore[assignment]
    daemon._gate = SilenceGate(enabled=True, mode="auto", manual_threshold_db=-45)
    daemon._take_id = "take-1"
    daemon._uplink_ready = tail_start is None
    daemon._tail_start = tail_start
    daemon._recordings_dir = directory
    daemon._writer = recordings.RecordingWriter(directory, "take-1")
    daemon.state = State.RECORDING
    daemon.pump_task = asyncio.create_task(daemon._pump_audio())
    daemon.play_sound = lambda _f: None  # type: ignore[method-assign]
    daemon.emit_state = lambda *_a: None  # type: ignore[method-assign]
    daemon._committed_segments = {(0, 0): (800, "live text")}
    daemon.current_text = "live text"
    if backfill_ok is not None:
        daemon._backfills = [asyncio.create_task(asyncio.sleep(0, backfill_ok))]
    sink = copied if copied is not None else []

    async def copy(text: str) -> bool:
        sink.append(text)
        return True

    def transcribe(pcm: bytes, *rest: str) -> str:
        if requests is not None:
            requests.append((len(pcm), rest[-1]))
        if batch is None:
            raise ConnectionError("backend down")
        return batch

    daemon.copy_to_clipboard = copy  # type: ignore[method-assign]
    with (
        unittest.mock.patch.object(daemon_module, "STOP_TAIL_S", 0.01),
        unittest.mock.patch.object(daemon_module, "resolve_token", lambda _c: "token"),
        unittest.mock.patch.object(daemon_module.batch, "transcribe", transcribe),
    ):
        await daemon.stop_recording()
        if daemon._finish_task:
            await daemon._finish_task
    return daemon, voice


class TestStopSequence(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    async def test_every_pending_chunk_is_sent_before_the_gate_closes_and_bye_follows_receipt(
        self,
    ) -> None:
        _, voice = await _stop("committed", self.dir)
        tail = ["close_gate", "wait_take_done", "bye", "close"]
        self.assertEqual(voice.calls, ["audio"] * 5 + tail)
        self.assertEqual(voice.offsets, [i * CHUNK_BYTES // 2 for i in range(5)])
        (rec,) = recordings.list_recordings(self.dir)
        self.assertEqual(rec.duration_ms, 500)
        self.assertTrue(rec.transcript_complete)

    async def test_an_unavailable_receipt_replaces_the_live_text_with_the_batch_one(
        self,
    ) -> None:
        copied: list[str] = []
        daemon, _ = await _stop("unavailable", self.dir, copied=copied)
        self.assertEqual(daemon.state, State.IDLE)
        self.assertEqual(copied, ["batch text"])
        (rec,) = recordings.list_recordings(self.dir)
        self.assertEqual((rec.transcript, rec.transcript_source), ("batch text", "batch"))
        self.assertTrue(rec.transcript_complete)

    async def test_no_receipt_still_closes_cleanly_and_falls_back_to_the_batch(self) -> None:
        daemon, voice = await _stop(None, self.dir)
        self.assertEqual(voice.calls[-2:], ["bye", "close"])
        self.assertEqual(daemon.current_text, "batch text")

    async def test_a_socket_still_down_at_stop_gets_the_rest_after_one_marker(
        self,
    ) -> None:
        copied: list[str] = []
        requests: list[tuple[int, str]] = []
        await _stop(None, self.dir, tail_start=1600, copied=copied, requests=requests)
        mark = daemon_module.INTERRUPTED_MARK
        self.assertEqual(copied, [f"live text {mark} batch text"])
        self.assertEqual(requests, [(5 * CHUNK_BYTES - 1600 * 2, "live text")])
        (rec,) = recordings.list_recordings(self.dir)
        self.assertEqual(rec.transcript_source, "live+batch")
        self.assertTrue(rec.transcript_complete)

    async def test_backfilled_gaps_with_a_clean_receipt_need_no_further_batch(self) -> None:
        copied: list[str] = []
        requests: list[tuple[int, str]] = []
        await _stop("committed", self.dir, backfill_ok=True, copied=copied, requests=requests)
        self.assertEqual((copied, requests), (["live text"], []))
        self.assertTrue(recordings.list_recordings(self.dir)[0].transcript_complete)

    async def test_a_failed_backfill_transcribes_the_whole_recording(self) -> None:
        copied: list[str] = []
        requests: list[tuple[int, str]] = []
        await _stop("committed", self.dir, backfill_ok=False, copied=copied, requests=requests)
        self.assertEqual(copied, ["batch text"])
        self.assertEqual(requests, [(5 * CHUNK_BYTES, "")])

    async def test_a_failed_batch_keeps_the_live_text_incomplete(self) -> None:
        copied: list[str] = []
        daemon, _ = await _stop("unavailable", self.dir, batch=None, copied=copied)
        self.assertEqual(copied, ["live text"])
        self.assertEqual(daemon.state, State.IDLE)
        self.assertFalse(recordings.list_recordings(self.dir)[0].transcript_complete)


class TestTranscriptTake(unittest.TestCase):
    def test_frames_of_another_take_are_dropped_and_finals_order_by_end_sample(self) -> None:
        daemon = PaiSttDaemon({})
        daemon._take_id = "mine"
        daemon.emit_state = lambda *_a: None  # type: ignore[method-assign]
        daemon._on_transcript("tail", True, 5, 90000, "mine")
        daemon._on_transcript("head", True, 9, 40000, "mine")
        daemon._on_transcript("stale", True, 1, 10, "old")
        self.assertEqual(daemon.current_text, "head tail")


if __name__ == "__main__":
    unittest.main()
