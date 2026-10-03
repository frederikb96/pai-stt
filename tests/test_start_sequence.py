"""Start sequence: capture and the local recording begin before the socket is ready."""

import asyncio
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

    async def connect(self) -> None:
        await self.release.wait()
        if self.fail:
            raise ConnectionError("backend down")
        self.calls.append("connect")

    async def open_gate(self, reason: str, take_id: Optional[str] = None) -> str:
        self.calls.append("open_gate")
        return take_id or ""

    async def send_audio(self, offset: int, pcm: bytes) -> None:
        self.calls.append(f"audio@{offset}")

    async def send_silence(self, at_sample: int) -> None:
        self.calls.append("silence")

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

    async def _start(self, voice: SlowVoice) -> PaiSttDaemon:
        daemon = PaiSttDaemon(
            {
                "transcription_timeout": 5,
                "token_command": "sh -c 'printf fake-token'",
                "pai_cloud": {"socket_url": "wss://example/socket"},
                "silence_gate": {"enabled": False, "mode": "auto", "manual_threshold_db": -45},
            }
        )
        daemon.play_sound = lambda _f: None  # type: ignore[method-assign]
        daemon.emit_state = lambda *_a: None  # type: ignore[method-assign]

        async def copy(_text: str) -> bool:
            return True

        daemon.copy_to_clipboard = copy  # type: ignore[method-assign]

        async def spawn(*_a: Any, **_k: Any) -> LivePwRecord:
            return self.pw

        patches = [
            unittest.mock.patch.object(daemon_module, "OUTPUT_DIR", self.dir),
            unittest.mock.patch.object(daemon_module, "RECORDINGS_DIR", self.dir),
            unittest.mock.patch.object(daemon_module, "RESULT_SYMLINK", self.dir / "last"),
            unittest.mock.patch.object(daemon_module, "ensure_output_dir", lambda: None),
            unittest.mock.patch.object(daemon_module, "device_name", lambda: "test"),
            unittest.mock.patch.object(daemon_module, "default_mic", lambda: None),
            unittest.mock.patch.object(daemon_module, "STOP_TAIL_S", 0.01),
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

    async def test_failed_connect_keeps_the_local_recording(self) -> None:
        voice = SlowVoice(fail=True)
        daemon = await self._start(voice)
        self.pw.stdout.feed_data(bytes(CHUNK_BYTES * 4))
        voice.release.set()
        await self._settle()
        ok, _ = await daemon.stop_recording()
        self.assertTrue(ok)
        self.assertEqual(daemon.state, State.IDLE)
        self.assertNotIn("audio@0", voice.calls)
        (rec,) = recordings.list_recordings(self.dir)
        self.assertEqual(rec.duration_ms, 400)
        self.assertFalse(rec.transcript_complete)


if __name__ == "__main__":
    unittest.main()
