"""Start sequence: capture and the local recording begin before the socket is ready."""

import asyncio
import json
import struct
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any, Optional

from pai_stt import daemon as daemon_module
from pai_stt import recordings, takes
from pai_stt.daemon import CHUNK_BYTES, PaiSttDaemon, State
from pai_stt.voice_client import TakeDone


class LivePwRecord:
    """A pw-record the test feeds by hand."""

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.pid = 1

    def terminate(self) -> None:
        self.stdout.feed_eof()

    def kill(self) -> None:
        self.stdout.feed_eof()

    async def wait(self) -> int:
        return 0


class SlowVoice:
    """A voice client whose connect() finishes only when the test releases it."""

    def __init__(self, fail: bool) -> None:
        self.release = asyncio.Event()
        self.fail = fail
        self.calls: list[str] = []
        self.silence_allowed = False
        self.sent: list[bytes] = []
        self.resume_token: Optional[str] = None
        self.resumed = False
        self.oldest_unacked_offset: Optional[int] = None

    async def connect(self, resume_token: Optional[str] = None) -> None:
        await self.release.wait()
        if self.fail:
            raise ConnectionError("backend down")
        self.calls.append("connect")

    async def open_gate(self, reason: str, take_id: Optional[str] = None) -> str:
        self.calls.append("open_gate")
        return take_id or ""

    async def send_audio(self, offset: int, pcm: bytes) -> None:
        self.calls.append(f"audio@{offset}")
        self.sent.append(pcm)

    async def send_silence(self, at_sample: int) -> None:
        self.calls.append("silence")

    async def resend_unacked(self) -> int:
        return 0

    async def close_gate(self, reason: str) -> None:
        self.calls.append("close_gate")

    async def wait_take_done(self, take_id: str, timeout: float) -> Optional[TakeDone]:
        return TakeDone(take_id, 0, "committed")

    async def bye(self, reason: str) -> None:
        self.calls.append("bye")

    async def close(self) -> None:
        self.calls.append("close")


class DaemonHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.pw = LivePwRecord()
        self.spawned: list[list[str]] = []

    async def _start(
        self, voice: Optional[SlowVoice], gain_db: float = 0, mode: str = "live"
    ) -> PaiSttDaemon:
        """Start a take; `voice` None makes any voice socket the daemon creates an error."""
        self.copied: list[str] = []
        self.states: list[str] = []

        def make_voice(*_a: Any, **_k: Any) -> SlowVoice:
            if voice is None:
                raise AssertionError("a voice socket was created")
            return voice

        daemon = PaiSttDaemon(
            {
                "transcription_timeout": 5,
                "token_command": "sh -c 'printf fake-token'" if voice else "false",
                "pai_cloud": {"socket_url": "wss://example/socket"},
                "silence_gate": {"enabled": False, "mode": "auto", "manual_threshold_db": -45},
                "capture": {"gain_db": gain_db},
            }
        )
        daemon.play_sound = lambda _f: None  # type: ignore[method-assign]
        daemon.emit_state = (  # type: ignore[method-assign]
            lambda state, *_a: self.states.append(state)
        )

        async def copy(text: str) -> bool:
            self.copied.append(text)
            return True

        daemon.copy_to_clipboard = copy  # type: ignore[method-assign]

        async def spawn(*args: Any, **_k: Any) -> LivePwRecord:
            self.spawned.append(list(args))
            return self.pw

        patches = [
            unittest.mock.patch.object(daemon_module, "OUTPUT_DIR", self.dir),
            unittest.mock.patch.object(daemon_module, "RECORDINGS_DIR", self.dir),
            unittest.mock.patch.object(daemon_module, "RESULT_SYMLINK", self.dir / "last"),
            unittest.mock.patch.object(daemon_module, "ensure_output_dir", lambda: None),
            unittest.mock.patch.object(daemon_module, "device_name", lambda: "test"),
            unittest.mock.patch.object(daemon_module, "default_mic", lambda: None),
            unittest.mock.patch.object(daemon_module, "STOP_TAIL_S", 0.01),
            unittest.mock.patch.object(daemon_module, "_pw_record_supports_raw", lambda: True),
            unittest.mock.patch.object(daemon_module.asyncio, "create_subprocess_exec", spawn),
            unittest.mock.patch.object(daemon_module, "VoiceSocketClient", make_voice),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        ok, msg = await daemon.start_recording(mode)
        self.assertTrue(ok, msg)
        return daemon

    async def _settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)


class TestStartSequence(DaemonHarness):
    async def test_capture_is_started_with_the_raw_flag_so_no_header_reaches_the_pump(self) -> None:
        voice = SlowVoice(fail=False)
        voice.release.set()
        daemon = await self._start(voice)
        (argv,) = self.spawned
        self.assertEqual(argv[0], "pw-record")
        self.assertIn("--raw", argv)
        self.assertEqual(argv[-1], "-")
        await daemon.stop_recording()

    async def test_gain_applies_to_the_sent_audio_and_not_to_the_local_recording(self) -> None:
        voice = SlowVoice(fail=False)
        voice.release.set()
        daemon = await self._start(voice, gain_db=6.0206)  # x2
        self.pw.stdout.feed_data(struct.pack("<hhhh", 100, -100, 20000, -20000) * 400)
        await self._settle()
        await daemon.stop_recording()
        sent = b"".join(voice.sent)
        self.assertEqual(struct.unpack("<hhhh", sent[:8]), (200, -200, 32767, -32768))
        (wav,) = self.dir.glob("*.wav")
        recorded = wav.read_bytes()[recordings.WAV_HEADER_BYTES :]
        self.assertEqual(struct.unpack("<hhhh", recorded[:8]), (100, -100, 20000, -20000))

    async def test_frames_captured_before_ready_are_recorded_then_flushed_in_order(self) -> None:
        voice = SlowVoice(fail=False)
        daemon = await self._start(voice)
        for _ in range(3):
            self.pw.stdout.feed_data(bytes(CHUNK_BYTES))
        await self._settle()
        self.assertEqual(voice.calls, [])
        (wav,) = self.dir.glob("*.wav")
        self.assertEqual(wav.stat().st_size, recordings.WAV_HEADER_BYTES + 3 * CHUNK_BYTES)

        voice.release.set()
        await self._settle()
        self.pw.stdout.feed_data(bytes(CHUNK_BYTES))
        await self._settle()
        half = CHUNK_BYTES // 2
        self.assertEqual(
            voice.calls, ["connect", "open_gate"] + [f"audio@{i * half}" for i in range(4)]
        )
        await daemon.stop_recording()

    async def test_stop_before_ready_waits_for_the_connection_and_flushes(self) -> None:
        voice = SlowVoice(fail=False)
        daemon = await self._start(voice)
        self.pw.stdout.feed_data(bytes(CHUNK_BYTES * 2))
        await self._settle()
        asyncio.get_running_loop().call_later(0.05, voice.release.set)
        await daemon.stop_recording()
        self.assertEqual(
            voice.calls,
            ["connect", "open_gate", "audio@0", f"audio@{CHUNK_BYTES // 2}"]
            + ["close_gate", "bye", "close"],
        )
        self.assertEqual(daemon.state, State.IDLE)

    async def test_a_backend_down_for_the_whole_take_is_transcribed_from_the_recording(
        self,
    ) -> None:
        voice = SlowVoice(fail=True)
        daemon = await self._start(voice)
        self.pw.stdout.feed_data(bytes(CHUNK_BYTES * 4))
        voice.release.set()
        await self._settle()
        with unittest.mock.patch.object(
            daemon_module.batch, "transcribe", lambda pcm, *_a: f"{len(pcm)} bytes"
        ):
            ok, _ = await daemon.stop_recording()
            assert daemon._finish_task is not None
            await daemon._finish_task
        self.assertTrue(ok)
        self.assertEqual(daemon.state, State.IDLE)
        self.assertNotIn("audio@0", voice.calls)
        (rec,) = recordings.list_recordings(self.dir)
        self.assertEqual(rec.duration_ms, 400)
        mark = daemon_module.INTERRUPTED_MARK
        self.assertEqual(rec.transcript, f"{mark} {CHUNK_BYTES * 4} bytes")


class BatchVoice(SlowVoice):
    """A socket that holds a batch take: the seal is answered unless `seal_answered` is false."""

    def __init__(self, fail: bool, phase: str = "capturing", seal_answered: bool = True) -> None:
        super().__init__(fail)
        self.release.set()
        self.phase = phase
        self.seal_answered = seal_answered
        self.sealed_at: Optional[int] = None

    async def close_gate(
        self, reason: str, take_id: Optional[str] = None, captured_samples: Optional[int] = None
    ) -> None:
        self.calls.append("close_gate")
        self.sealed_at = captured_samples

    async def wait_take_state(self, take_id: str, timeout: float) -> Optional[dict[str, Any]]:
        return {"state": "sealed"} if self.seal_answered else None

    async def wait_take_done(self, take_id: str, timeout: float) -> Optional[TakeDone]:
        return None


class FakeTakes:
    """The backend's take routes: a take that reports `missing` until uploaded, then a text."""

    def __init__(self, text: str = "server words", state: str = "done") -> None:
        self.text = text
        self.final_state = state
        self.captured: Optional[int] = None
        self.received: list[tuple[int, int]] = []
        self.calls: list[str] = []

    def _view(self) -> takes.TakeView:
        assert self.captured is not None
        missing = [] if self.received else ([(0, self.captured)] if self.captured else [])
        state = self.final_state if not missing else "sealed"
        text = self.text if state == "done" else None
        return takes.TakeView(state, missing, text, None, None)

    def get(self, take_id: str, deadline: float) -> takes.TakeView:
        self.calls.append("get")
        return self._view()

    def seal(self, take_id: str, captured: int, device: str, deadline: float) -> takes.TakeView:
        self.calls.append("seal")
        self.captured = captured
        return self._view()

    def upload(self, take_id: str, pcm: bytes, start: int, deadline: float) -> None:
        self.calls.append("upload")
        self.received.append((start, start + len(pcm) // 2))


class TestBatchTake(DaemonHarness):
    def setUp(self) -> None:
        super().setUp()
        for name, value in (
            ("TAKE_POLL_FIRST_S", 0.01),
            ("TAKE_POLL_MAX_S", 0.05),
            # A take that never completes fails the test instead of hanging it.
            ("FINISH_DEADLINE_S", 3),
        ):
            patch = unittest.mock.patch.object(daemon_module, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    async def _record_batch(
        self, voice: SlowVoice, server: FakeTakes, chunks: int = 3
    ) -> PaiSttDaemon:
        daemon = await self._start(voice, mode="batch")
        daemon._take_client = lambda: server  # type: ignore[assignment, method-assign, return-value]
        self.pw.stdout.feed_data(bytes(CHUNK_BYTES * chunks))
        await self._settle()
        return daemon

    async def _stop(self, daemon: PaiSttDaemon) -> tuple[bool, str]:
        result = await daemon.stop_recording()
        self.assertEqual(daemon.state, State.IDLE)
        assert daemon._finish_task is not None
        await daemon._finish_task
        return result

    async def test_a_take_sealed_over_the_socket_needs_no_upload_and_its_text_is_delivered(
        self,
    ) -> None:
        voice, server = BatchVoice(fail=False), FakeTakes()
        daemon = await self._record_batch(voice, server)
        server.captured = CHUNK_BYTES * 3 // 2
        server.received = [(0, server.captured)]
        await self._stop(daemon)
        self.assertEqual(voice.sealed_at, CHUNK_BYTES * 3 // 2)
        self.assertEqual(
            [c for c in voice.calls if c.startswith("audio")][-1], f"audio@{CHUNK_BYTES}"
        )
        self.assertNotIn("seal", server.calls)
        self.assertNotIn("upload", server.calls)
        self.assertEqual((self.copied, self.states[-1]), (["server words"], "done"))
        (rec,) = recordings.list_recordings(self.dir)
        self.assertEqual(
            (rec.mode, rec.transcript, rec.transcript_source), ("batch", "server words", "server")
        )
        self.assertTrue(rec.transcript_complete)

    async def test_with_the_socket_down_the_take_is_sealed_over_rest_and_uploaded_whole(
        self,
    ) -> None:
        server = FakeTakes()
        daemon = await self._record_batch(BatchVoice(fail=True), server)
        await self._stop(daemon)
        self.assertEqual(server.calls[:2], ["seal", "upload"])
        self.assertEqual(
            (server.captured, server.received), (CHUNK_BYTES * 3 // 2, [(0, CHUNK_BYTES * 3 // 2)])
        )
        self.assertEqual(self.copied, ["server words"])

    async def test_a_backend_answering_with_a_live_engine_is_dropped_and_rest_takes_over(
        self,
    ) -> None:
        voice, server = BatchVoice(fail=False, phase="dictating"), FakeTakes()
        daemon = await self._record_batch(voice, server, chunks=0)
        daemon._on_state({"phase": "dictating"})
        self.pw.stdout.feed_data(bytes(CHUNK_BYTES * 2))
        await self._settle()
        await self._stop(daemon)
        self.assertIsNone(voice.sealed_at)
        self.assertEqual(server.calls[:2], ["seal", "upload"])
        self.assertEqual(server.received, [(0, CHUNK_BYTES)])

    async def test_a_failed_take_reports_an_error_and_keeps_the_recording_for_a_retry(
        self,
    ) -> None:
        server = FakeTakes(state="failed")
        daemon = await self._record_batch(BatchVoice(fail=True), server, chunks=2)
        await self._stop(daemon)
        self.assertEqual((self.copied, self.states[-1]), ([], "error"))
        (rec,) = recordings.list_recordings(self.dir)
        self.assertFalse(rec.transcript_complete)
        with (
            unittest.mock.patch.object(daemon_module.batch, "transcribe", lambda *_a: "second try"),
            unittest.mock.patch.object(daemon_module, "resolve_token", lambda _c: "token"),
        ):
            self.assertTrue(daemon.retranscribe(rec.id[:8]))
            for _ in range(50):
                if not daemon._retranscribing:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(self.copied, ["second try"])

    async def test_an_unfinished_recent_batch_take_is_finished_at_startup(self) -> None:
        server = FakeTakes(text="later words")
        daemon = await self._record_batch(BatchVoice(fail=True), server, chunks=2)
        # The daemon dies before the finish runs: the recording is written, the take owed.
        assert daemon._writer is not None
        daemon._writer.finish("", False)
        live = recordings.RecordingWriter(self.dir, "live-take", "live")
        live.append(bytes(CHUNK_BYTES))
        live.finish("", False)
        await daemon.resume_owed_takes()
        by_mode = {r.mode: r for r in recordings.list_recordings(self.dir)}
        self.assertEqual(by_mode["batch"].transcript, "later words")
        self.assertFalse(by_mode["live"].transcript_complete)
        self.assertEqual(server.received, [(0, CHUNK_BYTES)])
        self.assertEqual(self.copied, [])

    async def test_the_batch_shortcut_stops_a_live_take(self) -> None:
        voice = SlowVoice(fail=False)
        voice.release.set()
        daemon = await self._start(voice)
        self.pw.stdout.feed_data(bytes(CHUNK_BYTES))
        await self._settle()
        reply = await daemon.handle_command("toggle-batch")
        self.assertTrue(reply.startswith("OK"), reply)
        self.assertEqual(daemon.state, State.IDLE)
        self.assertIn("close_gate", voice.calls)

    async def test_the_recording_being_written_cannot_be_retranscribed(self) -> None:
        daemon = await self._record_batch(BatchVoice(fail=False), FakeTakes(), chunks=1)
        (rec,) = recordings.list_recordings(self.dir)
        self.assertFalse(daemon.retranscribe(rec.id))
        self.assertTrue(json.loads(daemon.recordings_json())[0]["recording"])


if __name__ == "__main__":
    unittest.main()
