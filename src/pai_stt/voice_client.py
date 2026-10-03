"""Client for the PAI Cloud voice socket.

Speaks the documented protocol for `GET /api/voice/socket`: the `hello` /
`ready` handshake, the uplink gate and audio frames, the liveness `ping`/
`pong` pair, and the downlink messages a transport with no playback channel
receives (`ack`, `state`, `notice`, `clear`, `transcript`). See the backend's
own protocol document for the full frame set and what each field means; this
module does not repeat it.

Declaring `caps.audio_downlink: false` on `hello` is what makes the backend
attach this bus to its transcription-only engine and route transcribed text
back as `transcript` frames instead of trying to speak it — there is no
separate opt-in.

`connect()` starts the receive loop as a task this client owns and returns
once `ready` has arrived, so the liveness `ping` is answered for as long as
the connection lives. `_dispatch` logs anything it does not recognise rather
than raising, so a frame type added later degrades to a warning instead of a
crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

import websockets

from pai_stt.framing import decode_down_frame, encode_up_frame

logger = logging.getLogger("pai_stt.voice_client")

# The transport identifier this client sends in `hello.transport`.
TRANSPORT_NAME = "pai-stt"

# How long `connect()` waits for the backend's `ready` before giving up.
READY_TIMEOUT_S = 10.0


def _noop(*_args: Any) -> None:
    return None


@dataclass
class VoiceSocketCallbacks:
    """Handlers for the downlink messages this client dispatches.

    Each defaults to a no-op so a caller only wires up what it needs. There
    is no `on_ping` — a `ping` is answered with `pong` internally, since
    liveness is this module's own concern, not something a caller can get
    wrong by forgetting to reply.
    """

    on_ready: Callable[[dict[str, Any]], None] = _noop
    on_ack: Callable[[int], None] = _noop
    on_state: Callable[[dict[str, Any]], None] = _noop
    on_notice: Callable[[dict[str, Any]], None] = _noop
    on_clear: Callable[[], None] = _noop
    on_audio: Callable[[int, bytes], None] = _noop
    #: `text`, `is_final`, `seq` from a `transcript` down frame. The caller
    #: renders every `is_final` segment it has seen, in `seq` order,
    #: followed by the latest non-final one — the composition rule the
    #: protocol document specifies, not something this client resolves on
    #: the caller's behalf, since only the caller knows where the result
    #: goes (clipboard, file, panel preview).
    #: Also receives the frame's `end_sample` (finals only) and `take_id`.
    on_transcript: Callable[[str, bool, int, Optional[int], Optional[str]], None] = _noop
    #: The receive loop ended: the socket closed or failed.
    on_closed: Callable[[], None] = _noop


@dataclass(frozen=True)
class TakeDone:
    """The `take_done` receipt: nothing more is coming for `take_id`."""

    take_id: str
    final_seq: int
    ended: str


class VoiceSocketClient:
    """One connection to the voice socket, from hello through to bye."""

    def __init__(
        self,
        url: str,
        token: str,
        callbacks: VoiceSocketCallbacks,
        *,
        device_name: str,
        mic: Optional[str],
        silence_gate: bool,
    ) -> None:
        self._url = url
        self._token = token
        self._callbacks = callbacks
        self._device = {"name": device_name, "mic": mic}
        self._silence_gate = silence_gate
        self._ws: Optional[websockets.ClientConnection] = None
        self._seq = 0
        self._resume_token: Optional[str] = None
        self._silence_allowed = False
        self._ready = asyncio.Event()
        self._reader: Optional[asyncio.Task[None]] = None
        self._take_futures: dict[str, asyncio.Future[Optional[TakeDone]]] = {}

    @property
    def silence_allowed(self) -> bool:
        """The backend's latest `silence_allowed` (false until it says otherwise)."""
        return self._silence_allowed

    @property
    def resume_token(self) -> Optional[str]:
        """The token a later `connect()` can present to reattach to this bus."""
        return self._resume_token

    async def connect(self, resume_token: Optional[str] = None) -> None:
        """Open the socket, send `hello`, start reading and wait for `ready`."""
        self._ws = await websockets.connect(self._url)
        self._seq = 0
        self._ready.clear()
        hello: dict[str, Any] = {
            "type": "hello",
            "transport": TRANSPORT_NAME,
            # No audio downlink: this client only ever writes transcribed
            # text, never plays synthesised speech back.
            "caps": {"audio_downlink": False, "silence_gate": self._silence_gate},
            "device": self._device,
            "auth": self._token,
        }
        if resume_token:
            hello["resume_token"] = resume_token
        await self._ws.send(json.dumps(hello))
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._ready.wait(), READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.close()
            raise ConnectionError("voice socket sent no ready") from None

    async def _read_loop(self) -> None:
        """Read frames until the connection closes, dispatching to callbacks."""
        assert self._ws is not None
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    ref, pcm = decode_down_frame(message)
                    self._callbacks.on_audio(ref, pcm)
                else:
                    await self._dispatch(json.loads(message))
        except websockets.ConnectionClosed:
            pass
        except Exception:
            logger.exception("Voice socket read loop failed")
        finally:
            for future in self._take_futures.values():
                if not future.done():
                    future.set_result(None)
            self._callbacks.on_closed()

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "ready":
            self._resume_token = msg.get("resume_token")
            self._silence_allowed = bool(msg.get("silence_allowed", False))
            self._ready.set()
            self._callbacks.on_ready(msg)
        elif msg_type == "ack":
            self._callbacks.on_ack(msg["through_seq"])
        elif msg_type == "state":
            self._silence_allowed = bool(msg.get("silence_allowed", False))
            self._callbacks.on_state(msg)
        elif msg_type == "notice":
            self._callbacks.on_notice(msg)
        elif msg_type == "clear":
            self._callbacks.on_clear()
        elif msg_type == "transcript":
            self._callbacks.on_transcript(
                msg["text"], msg["is_final"], msg["seq"], msg.get("end_sample"), msg.get("take_id")
            )
        elif msg_type == "take_done":
            done = TakeDone(msg["take_id"], msg["final_seq"], msg["ended"])
            future = self._take_future(done.take_id)
            if not future.done():
                future.set_result(done)
        elif msg_type == "ping":
            # Timestamp-free by design: the backend judges round-trip time
            # server-side from when it sent the ping, not from anything
            # this reply carries.
            await self._send_json({"type": "pong"})
        else:
            logger.warning("Unhandled voice socket message: %s", msg_type)

    def _take_future(self, take_id: str) -> asyncio.Future[Optional[TakeDone]]:
        return self._take_futures.setdefault(take_id, asyncio.get_running_loop().create_future())

    async def open_gate(self, reason: str, take_id: Optional[str] = None) -> str:
        """Start a take and return its `take_id`, minted here unless the caller already holds
        one. Resets the frame counter."""
        take_id = take_id or str(uuid.uuid4())
        self._take_future(take_id)
        await self._send_json({"type": "gate", "open": True, "reason": reason, "take_id": take_id})
        self._seq = 0
        return take_id

    async def close_gate(self, reason: str) -> None:
        """Declare that this take has ended."""
        await self._send_json({"type": "gate", "open": False, "reason": reason})

    async def send_silence(self, at_sample: int) -> None:
        """Declare that audio is being withheld from `at_sample` on; the take continues."""
        await self._send_json({"type": "silence", "at_sample": at_sample})

    async def wait_take_done(self, take_id: str, timeout: float) -> Optional[TakeDone]:
        """The `take_done` receipt for `take_id`, or None on timeout or a closed socket."""
        try:
            return await asyncio.wait_for(asyncio.shield(self._take_future(take_id)), timeout)
        except asyncio.TimeoutError:
            return None

    async def send_audio(self, sample_offset: int, pcm: bytes) -> None:
        """Send one uplink audio frame while the gate is open."""
        if self._ws is None:
            raise RuntimeError("connect() must run before send_audio()")
        await self._ws.send(encode_up_frame(self._seq, sample_offset, pcm))
        self._seq += 1

    async def played(self, ref: int) -> None:
        """Acknowledge that downlink audio carrying `ref` finished playing."""
        await self._send_json({"type": "played", "ref": ref})

    async def bye(self, reason: str) -> None:
        """Tell the backend this connection is ending on purpose."""
        await self._send_json({"type": "bye", "reason": reason})

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._reader is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("connect() must run before sending")
        await self._ws.send(json.dumps(payload))
