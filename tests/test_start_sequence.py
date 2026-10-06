"""Start sequence: capture and the local recording begin before the socket is ready."""

import asyncio
import struct
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any, Optional

from pai_stt import daemon as daemon_module
from pai_stt import recordings
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


class TestStartSequence(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.pw = LivePwRecord()
        self.spawned: list[list[str]] = []

    async def _start(self, voice: SlowVoice, gain_db: float = 0) -> PaiSttDaemon:
        daemon = PaiSttDaemon(
            {
                "transcription_timeout": 5,
                "token_command": "sh -c 'printf fake-token'",
                "pai_cloud": {"socket_url": "wss://example/socket"},
                "silence_gate": {"enabled": False, "mode": "auto", "manual_threshold_db": -45},
                "capture": {"gain_db": gain_db},
            }
        )
        daemon.play_sound = lambda _f: None  # type: ignore[method-assign]
        daemon.emit_state = lambda *_a: None  # type: ignore[method-assign]

        async def copy(_text: str) -> bool:
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
            unittest.mock.patch.object(daemon_module, "VoiceSocketClient", lambda *_a, **_k: voice),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        ok, msg = await daemon.start_recording()
        self.assertTrue(ok, msg)
        return daemon

    async def _settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)

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


if __name__ == "__main__":
    unittest.main()
