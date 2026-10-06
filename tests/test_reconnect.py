"""A voice socket that drops mid-recording is reconnected and nothing captured is lost."""

import asyncio
import json
import struct
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any, Optional

from websockets.asyncio.server import ServerConnection, serve

from pai_stt import daemon as daemon_module
from pai_stt import recordings
from pai_stt.daemon import CHUNK_BYTES, PaiSttDaemon, State
from pai_stt.framing import encode_up_frame
from pai_stt.silence_gate import Frame, SilenceGate
from pai_stt.voice_client import VoiceSocketCallbacks, VoiceSocketClient

LOUD = struct.pack("<h", 8000) * (CHUNK_BYTES // 2)


class DroppingServer:
    """Acks only the first frame, then drops the first socket after three frames."""

    def __init__(self) -> None:
        self.hellos: list[dict[str, Any]] = []
        self.frames: list[bytes] = []
        self.sockets = 0

    async def handler(self, ws: ServerConnection) -> None:
        self.sockets += 1
        first = self.sockets == 1
        async for raw in ws:
            if isinstance(raw, bytes):
                self.frames.append(raw)
                if first and len(self.frames) == 1:
                    await ws.send(json.dumps({"type": "ack", "through_seq": 0}))
                if first and len(self.frames) == 3:
                    await ws.close()
                    return
                continue
            msg = json.loads(raw)
            if msg["type"] == "hello":
                self.hellos.append(msg)
                ready = {"type": "ready", "resume_token": f"t{self.sockets}"}
                ready.update(resumed=not first, silence_allowed=True)
                await ws.send(json.dumps(ready))


class TestVoiceClientResend(unittest.IsolatedAsyncioTestCase):
    async def test_unacked_frames_are_resent_renumbered_after_a_resumed_reconnect(self) -> None:
        server = DroppingServer()
        closed = asyncio.Event()
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            client = VoiceSocketClient(
                f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}",
                "token",
                VoiceSocketCallbacks(on_closed=closed.set),
                device_name="host",
                mic=None,
                silence_gate=True,
            )
            await client.connect()
            for i in range(3):
                await client.send_audio(i * 100, bytes([i]) * 4)
            await asyncio.wait_for(closed.wait(), 2)
            await client.close()
            await client.connect(resume_token=client.resume_token)
            resent = await client.resend_unacked()
            await asyncio.sleep(0.05)
            await client.close()
        self.assertEqual(server.hellos[1]["resume_token"], "t1")
        self.assertTrue(client.resumed)
        self.assertEqual(resent, 2)
        self.assertEqual(
            server.frames[3:],
            [encode_up_frame(0, 100, bytes([1]) * 4), encode_up_frame(1, 200, bytes([2]) * 4)],
        )


class FakeVoice:
    """Mirrors the client's hold/resend contract; `connect` answers from a script."""

    def __init__(self, outcomes: list[Optional[bool]]) -> None:
        # Per connect attempt: resumed True/False, or None to fail.
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.silence_allowed = False
        self.resume_token = "t1"
        self.resumed = False
        self.unacked: list[tuple[int, bytes]] = []
        self.down = False

    async def close(self) -> None:
        self.calls.append("close")

    async def connect(self, resume_token: Optional[str] = None) -> None:
        outcome = self.outcomes.pop(0)
        if outcome is None:
            self.calls.append("connect-failed")
            raise ConnectionError("still down")
        self.calls.append(f"connect({resume_token})")
        self.resumed = outcome
        self.down = False

    async def open_gate(self, reason: str, take_id: Optional[str] = None) -> str:
        self.calls.append(f"open_gate({take_id})")
        return take_id or ""

    async def send_audio(self, offset: int, pcm: bytes) -> None:
        self.unacked.append((offset, pcm))
        if self.down:
            raise ConnectionError("socket closed")
        self.calls.append(f"audio@{offset}")

    def hold(self, frames: list[tuple[int, bytes]]) -> None:
        self.unacked.extend(frames)

    async def resend_unacked(self) -> int:
        backlog, self.unacked = self.unacked, []
        for offset, pcm in backlog:
            await self.send_audio(offset, pcm)
        return len(backlog)

    async def send_silence(self, at_sample: int) -> None:
        self.calls.append("silence")

    @property
    def oldest_unacked_offset(self) -> Optional[int]:
        return self.unacked[0][0] if self.unacked else None


def _recording_daemon(voice: FakeVoice) -> PaiSttDaemon:
    daemon = PaiSttDaemon({"pai_cloud": {"socket_url": "wss://example/socket"}})
    daemon.voice = voice  # type: ignore[assignment]
    daemon._gate = SilenceGate(enabled=False, mode="auto", manual_threshold_db=-45)
    daemon._take_id = "take-1"
    daemon._uplink_ready = True
    daemon.state = State.RECORDING
    daemon.play_sound = lambda _f: None  # type: ignore[method-assign]
    daemon.emit_state = lambda *_a: None  # type: ignore[method-assign]
    return daemon


async def _capture(daemon: PaiSttDaemon, first: int, count: int) -> None:
    for i in range(first, first + count):
        daemon._pending.append(Frame(i * CHUNK_BYTES // 2, LOUD))
        await daemon._drain_pending()


class TestDaemonReconnect(unittest.IsolatedAsyncioTestCase):
    async def test_a_resumed_bus_gets_the_lost_frame_then_the_queue_without_a_new_gate(
        self,
    ) -> None:
        voice = FakeVoice([True])
        daemon = _recording_daemon(voice)
        await _capture(daemon, 0, 2)
        voice.unacked = voice.unacked[1:]  # frame 0 acked, frame 1 in flight
        voice.down = True
        await _capture(daemon, 2, 1)  # the send that discovers the drop
        await _capture(daemon, 3, 2)  # captured while reconnecting
        assert daemon._reconnect_task is not None
        await daemon._reconnect_task
        await daemon._drain_pending()
        step = CHUNK_BYTES // 2
        self.assertNotIn("open_gate(take-1)", voice.calls)
        after = voice.calls[voice.calls.index("connect(t1)") + 1 :]
        self.assertEqual(after, [f"audio@{i * step}" for i in (1, 2, 3, 4)])
        self.assertTrue(daemon._uplink_ready)

    async def test_a_fresh_bus_reopens_the_take_and_keeps_the_text_it_already_had(self) -> None:
        voice = FakeVoice([None, False])
        daemon = _recording_daemon(voice)
        daemon._on_transcript("first", True, 0, 16000, "take-1")
        daemon._on_transcript("never committed", False, 1, None, "take-1")
        daemon._sent_through = 32000
        with unittest.mock.patch.object(daemon_module, "RECONNECT_MAX_DELAY_S", 0.01):
            daemon._on_closed()
            assert daemon._reconnect_task is not None
            await daemon._reconnect_task
        self.assertIn("open_gate(take-1)", voice.calls)
        daemon._on_transcript("after", True, 0, 48000, "take-1")
        self.assertEqual(daemon.current_text, "first never committed after")

    async def test_reconnect_retries_until_stop_then_makes_one_last_attempt(self) -> None:
        voice = FakeVoice([None] * 50)
        daemon = _recording_daemon(voice)
        daemon._pending = [Frame(4800, LOUD)]
        with unittest.mock.patch.object(daemon_module, "RECONNECT_MAX_DELAY_S", 0.01):
            daemon._on_closed()
            assert daemon._reconnect_task is not None
            await asyncio.sleep(0.2)
            self.assertFalse(daemon._reconnect_task.done())
            attempts = voice.calls.count("connect-failed")
            daemon._stopping.set()
            await daemon._reconnect_task
        self.assertLessEqual(voice.calls.count("connect-failed") - attempts, 2)
        self.assertFalse(daemon._uplink_ok)
        self.assertEqual(daemon._tail_start, 4800)

    async def test_pressing_stop_retries_at_once_instead_of_waiting_out_the_backoff(
        self,
    ) -> None:
        voice = FakeVoice([None, True])
        daemon = _recording_daemon(voice)
        with unittest.mock.patch.object(daemon_module, "RECONNECT_MAX_DELAY_S", 60):
            daemon._on_closed()
            assert daemon._reconnect_task is not None
            await asyncio.sleep(0.05)
            daemon._stopping.set()
            await asyncio.wait_for(daemon._reconnect_task, 0.1)
        self.assertTrue(daemon._uplink_ready)
        self.assertEqual(daemon._backfills, [])

    async def test_a_failed_first_connect_is_retried_like_a_drop(self) -> None:
        voice = FakeVoice([None, False])
        daemon = _recording_daemon(voice)
        daemon._uplink_ready = False
        with unittest.mock.patch.object(daemon_module, "RECONNECT_MAX_DELAY_S", 0.01):
            await daemon._connect_uplink(voice, "take-1")  # type: ignore[arg-type]
            assert daemon._reconnect_task is not None
            await daemon._reconnect_task
        self.assertIn("open_gate(take-1)", voice.calls)
        self.assertTrue(daemon._uplink_ready)

    def test_a_backlog_longer_than_a_replay_keeps_only_its_last_second(self) -> None:
        daemon = _recording_daemon(FakeVoice([]))
        step = CHUNK_BYTES // 2
        frames = int(daemon_module.REPLAY_MAX_S * 10) + 50
        daemon._pending = [Frame(i * step, LOUD) for i in range(frames)]
        skipped = daemon._trim_backlog()
        self.assertEqual(len(daemon._pending), 10)
        self.assertEqual(skipped, frames - 10)
        self.assertEqual(daemon._pending[-1].offset, (frames - 1) * step)
        self.assertEqual(daemon._gap_start, 0)

    def test_a_backlog_within_a_replay_is_sent_whole(self) -> None:
        daemon = _recording_daemon(FakeVoice([]))
        daemon._pending = [Frame(i * CHUNK_BYTES // 2, LOUD) for i in range(100)]
        self.assertEqual(daemon._trim_backlog(), 0)
        self.assertEqual(len(daemon._pending), 100)
        self.assertIsNone(daemon._gap_start)

    async def test_a_long_outage_is_backfilled_between_markers_once_the_socket_is_back(
        self,
    ) -> None:
        voice = FakeVoice([True])
        daemon = _recording_daemon(voice)
        step = CHUNK_BYTES // 2
        frames = int(daemon_module.REPLAY_MAX_S * 10) + 50
        with tempfile.TemporaryDirectory() as tmp:
            daemon._recordings_dir = Path(tmp)
            writer = recordings.RecordingWriter(Path(tmp), "take-1")
            writer.append(bytes(range(256)) * (frames * CHUNK_BYTES // 256))
            daemon._on_transcript("before", True, 0, 0, "take-1")
            daemon._pending = [Frame(i * step, LOUD) for i in range(frames)]
            requests: list[tuple[int, str]] = []
            release = asyncio.Event()

            async def transcribe(*args: Any) -> str:
                pcm, _take, _url, _token, previous = args
                requests.append((len(pcm), previous))
                await release.wait()
                return "gap words"

            with (
                unittest.mock.patch.object(daemon_module, "resolve_token", lambda _c: "t"),
                unittest.mock.patch.object(
                    daemon_module.asyncio, "to_thread", _to_thread(transcribe)
                ),
            ):
                daemon._uplink_ready = False
                await daemon._reconnect(voice, "take-1")  # type: ignore[arg-type]
                mark = daemon_module.INTERRUPTED_MARK
                self.assertEqual(daemon.current_text, f"before {mark} … {mark}")
                daemon._on_transcript("after", True, 1, frames * step, "take-1")
                release.set()
                (backfill,) = daemon._backfills
                self.assertTrue(await backfill)
            writer.finish("", False)
        kept = len(daemon._pending)
        self.assertEqual(requests, [((frames - kept) * step * 2, "before")])
        self.assertEqual(daemon.current_text, f"before {mark} gap words {mark} after")


def _to_thread(transcribe: Any) -> Any:
    """`asyncio.to_thread` that awaits `transcribe` for the batch call and runs the rest."""

    async def run(func: Any, *args: Any) -> Any:
        if func is daemon_module.batch.transcribe:
            return await transcribe(*args)
        return func(*args)

    return run

    async def test_a_drop_during_the_stop_sequence_does_not_reconnect(self) -> None:
        daemon = _recording_daemon(FakeVoice([]))
        daemon.state = State.TRANSCRIBING
        daemon._on_closed()
        self.assertIsNone(daemon._reconnect_task)


if __name__ == "__main__":
    unittest.main()
