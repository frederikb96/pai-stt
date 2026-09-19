"""Binary framing for the PAI Cloud voice socket.

Audio travels as binary WebSocket frames. An uplink frame is prefixed with the
client's own monotonically increasing sequence number and the sample offset
within the current take; a downlink frame carries only the opaque `ref` a
client echoes back in its next `played` control message. See the voice socket
protocol document for the frame semantics and control message shapes this
module does not cover.
"""

from __future__ import annotations

import struct

_UP_HEADER = struct.Struct(">II")
_DOWN_HEADER = struct.Struct(">I")


def encode_up_frame(seq: int, sample_offset: int, pcm: bytes) -> bytes:
    """Build an uplink audio frame: [u32 seq][u32 sample_offset][pcm16 samples]."""
    return _UP_HEADER.pack(seq, sample_offset) + pcm


def decode_down_frame(data: bytes) -> tuple[int, bytes]:
    """Split a downlink audio frame into its `ref` and PCM payload."""
    (ref,) = _DOWN_HEADER.unpack_from(data, 0)
    return ref, data[_DOWN_HEADER.size :]
