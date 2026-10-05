"""A voice socket that drops mid-recording is reconnected and nothing captured is lost."""

import asyncio
import json
import struct
import unittest
import unittest.mock
from typing import Any, Optional

from websockets.asyncio.server import ServerConnection, serve

from pai_stt import daemon as daemon_module
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

    async def test_reconnect_gives_up_after_its_window_and_recording_continues_locally(
        self,
    ) -> None:
        voice = FakeVoice([None] * 50)
        daemon = _recording_daemon(voice)
        with unittest.mock.patch.object(daemon_module, "RECONNECT_WINDOW_S", 0.05):
            daemon._on_closed()
            assert daemon._reconnect_task is not None
            await daemon._reconnect_task
        self.assertFalse(daemon._uplink_ok)
        self.assertFalse(daemon._uplink_ready)

    async def test_a_drop_during_the_stop_sequence_does_not_reconnect(self) -> None:
        daemon = _recording_daemon(FakeVoice([]))
        daemon.state = State.TRANSCRIBING
        daemon._on_closed()
        self.assertIsNone(daemon._reconnect_task)


if __name__ == "__main__":
    unittest.main()
