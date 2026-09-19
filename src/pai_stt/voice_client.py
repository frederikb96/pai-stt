"""Client for the PAI Cloud voice socket.

Speaks the documented protocol for `GET /api/voice/socket`: the `hello` /
`ready` handshake, the uplink gate and audio frames, and the downlink control
messages a transport with no playback channel still needs to see (`ack`,
`state`, `notice`, `clear`). See the backend's own protocol document for the
full frame set and what each field means; this module does not repeat it.

Two things the protocol document leaves open, both unimplemented here rather
than guessed at:

- How transcribed text reaches a transport whose `caps` declare no downlink
  at all. The document defines a binary audio frame down and a `state`
  message for phase changes, but no frame carrying transcript text.
- What an uplink reply to the backend's liveness ping looks like. The
  document says the backend "sends an application-level ping on a regular
  cadence"; no up-message type for a reply appears in its frame tables.

`_dispatch` logs anything it does not recognise rather than raising, so
either arriving later degrades to a warning instead of a crash.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

import websockets

from pai_stt.framing import decode_down_frame, encode_up_frame

logger = logging.getLogger("pai_stt.voice_client")

# The transport identifier this client sends in `hello.transport`. The
# protocol document's enum uses "voxscribe" for the Linux dictation client
# regardless of which repository or package implements it.
TRANSPORT_NAME = "voxscribe"


def _noop(*_args: Any) -> None:
    return None


@dataclass
class VoiceSocketCallbacks:
    """Handlers for the downlink messages this client dispatches.

    Each defaults to a no-op so a caller only wires up what it needs.
    """

    on_ready: Callable[[dict[str, Any]], None] = _noop
    on_ack: Callable[[int], None] = _noop
    on_state: Callable[[dict[str, Any]], None] = _noop
    on_notice: Callable[[dict[str, Any]], None] = _noop
    on_clear: Callable[[], None] = _noop
    on_audio: Callable[[int, bytes], None] = _noop


class VoiceSocketClient:
    """One connection to the voice socket, from hello through to bye."""

    def __init__(self, url: str, token: str, callbacks: VoiceSocketCallbacks) -> None:
        self._url = url
        self._token = token
        self._callbacks = callbacks
        self._ws: Optional[websockets.ClientConnection] = None
        self._seq = 0
        self._resume_token: Optional[str] = None

    @property
    def resume_token(self) -> Optional[str]:
        """The token a later `connect()` can present to reattach to this bus."""
        return self._resume_token

    async def connect(self, resume_token: Optional[str] = None) -> None:
        """Open the socket and send `hello`. Does not wait for `ready`."""
        self._ws = await websockets.connect(self._url)
        self._seq = 0
        hello: dict[str, Any] = {
            "type": "hello",
            "transport": TRANSPORT_NAME,
            # No audio downlink: this client only ever writes transcribed
            # text, never plays synthesised speech back.
            "caps": {"downlink": False},
            "auth": self._token,
        }
        if resume_token:
            hello["resume_token"] = resume_token
        await self._ws.send(json.dumps(hello))

    async def run(self) -> None:
        """Read frames until the connection closes, dispatching to callbacks."""
        if self._ws is None:
            raise RuntimeError("connect() must run before run()")
        async for message in self._ws:
            if isinstance(message, bytes):
                ref, pcm = decode_down_frame(message)
                self._callbacks.on_audio(ref, pcm)
            else:
                self._dispatch(json.loads(message))

    def _dispatch(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "ready":
            self._resume_token = msg.get("resume_token")
            self._callbacks.on_ready(msg)
        elif msg_type == "ack":
            self._callbacks.on_ack(msg["through_seq"])
        elif msg_type == "state":
            self._callbacks.on_state(msg)
        elif msg_type == "notice":
            self._callbacks.on_notice(msg)
        elif msg_type == "clear":
            self._callbacks.on_clear()
        else:
            logger.warning("Unhandled voice socket message: %s", msg_type)

    async def open_gate(self, reason: str) -> None:
        """Declare intent to start sending audio; resets the take's frame counter."""
        await self._send_json({"type": "gate", "open": True, "reason": reason})
        self._seq = 0

    async def close_gate(self, reason: str) -> None:
        """Declare that this take has ended."""
        await self._send_json({"type": "gate", "open": False, "reason": reason})

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

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("connect() must run before sending")
        await self._ws.send(json.dumps(payload))
