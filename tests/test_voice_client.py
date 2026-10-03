"""The voice client against a scripted loopback server."""

import asyncio
import json
import unittest
import unittest.mock
from typing import Any, Optional

from websockets.asyncio.server import ServerConnection, serve

from pai_stt.voice_client import VoiceSocketCallbacks, VoiceSocketClient


class Server:
    """Answers `hello` with `ready`, then plays a script; records what it receives."""

    def __init__(self, *, ready: bool = True, take_done_ended: Optional[str] = "committed") -> None:
        self.received: list[Any] = []
        self.pong = asyncio.Event()
        self._ready = ready
        self._ended = take_done_ended

    async def handler(self, ws: ServerConnection) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                self.received.append(raw)
                continue
            msg = json.loads(raw)
            self.received.append(msg)
            if msg["type"] == "hello" and self._ready:
                await ws.send(
                    json.dumps({"type": "ready", "resume_token": "t", "silence_allowed": True})
                )
                await ws.send(json.dumps({"type": "ping"}))
            elif msg["type"] == "pong":
                self.pong.set()
            elif msg["type"] == "gate" and not msg["open"] and self._ended:
                take_id = next(
                    m["take_id"] for m in self.received if m.get("type") == "gate" and m["open"]
                )
                done = {"type": "take_done", "take_id": take_id, "final_seq": 3}
                done["ended"] = self._ended
                await ws.send(json.dumps(done))


def _client(port: int) -> VoiceSocketClient:
    return VoiceSocketClient(
        f"ws://127.0.0.1:{port}",
        "token",
        VoiceSocketCallbacks(),
        device_name="host",
        mic="Headset",
        silence_gate=True,
    )


class TestVoiceClient(unittest.IsolatedAsyncioTestCase):
    async def test_hello_declares_device_and_gate_and_ping_is_answered(self) -> None:
        server = Server()
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            client = _client(srv.sockets[0].getsockname()[1])
            await client.connect()
            await asyncio.wait_for(server.pong.wait(), 2)
            await client.close()
        hello = server.received[0]
        self.assertEqual(hello["caps"], {"audio_downlink": False, "silence_gate": True})
        self.assertEqual(hello["device"], {"name": "host", "mic": "Headset"})
        self.assertTrue(client.silence_allowed)

    async def test_take_done_is_delivered_for_the_take_the_gate_opened(self) -> None:
        server = Server()
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            client = _client(srv.sockets[0].getsockname()[1])
            await client.connect()
            take_id = await client.open_gate("button")
            await client.close_gate("button")
            done = await client.wait_take_done(take_id, 2)
            await client.close()
        assert done is not None
        self.assertEqual((done.ended, done.final_seq), ("committed", 3))
        gate = next(m for m in server.received if m.get("type") == "gate" and m["open"])
        self.assertEqual(gate["take_id"], take_id)

    async def test_wait_take_done_gives_up_on_timeout(self) -> None:
        server = Server(take_done_ended=None)
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            client = _client(srv.sockets[0].getsockname()[1])
            await client.connect()
            take_id = await client.open_gate("button")
            self.assertIsNone(await client.wait_take_done(take_id, 0.1))
            await client.close()

    async def test_wait_take_done_returns_when_the_socket_drops(self) -> None:
        server = Server(take_done_ended=None)
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            client = _client(srv.sockets[0].getsockname()[1])
            await client.connect()
            take_id = await client.open_gate("button")
            waiter = asyncio.create_task(client.wait_take_done(take_id, 30))
            await asyncio.sleep(0.05)
            for conn in list(srv.connections):
                await conn.close()
            self.assertIsNone(await asyncio.wait_for(waiter, 2))
            await client.close()

    async def test_connect_fails_when_no_ready_arrives(self) -> None:
        server = Server(ready=False)
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            client = _client(srv.sockets[0].getsockname()[1])
            with unittest.mock.patch("pai_stt.voice_client.READY_TIMEOUT_S", 0.1):
                with self.assertRaises(ConnectionError):
                    await client.connect()


if __name__ == "__main__":
    unittest.main()
