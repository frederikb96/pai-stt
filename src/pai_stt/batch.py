"""Re-transcription of a stored recording through the backend's batch route.

The recording goes up in pieces of at most `PIECE_S` seconds that overlap by
`OVERLAP_S`, each as its own WAV, sequentially. Every request carries the
tail of the text so far as `previous_text`; the backend removes the overlap,
so the texts are simply joined with a space.
"""

from __future__ import annotations

import json
import urllib.request
import uuid
from urllib.parse import urlsplit, urlunsplit

from pai_stt.recordings import SAMPLE_RATE, wav_header

PIECE_S = 300
OVERLAP_S = 1
PREVIOUS_TEXT_CHARS = 200
REQUEST_TIMEOUT_S = 180


def http_base(socket_url: str) -> str:
    """The deployment's HTTP origin, derived from its voice socket URL."""
    parts = urlsplit(socket_url)
    scheme = {"wss": "https", "ws": "http"}[parts.scheme]
    return urlunsplit((scheme, parts.netloc, "", "", ""))


def pieces(pcm: bytes) -> list[bytes]:
    """Split samples into overlapping pieces of at most PIECE_S seconds."""
    piece = PIECE_S * SAMPLE_RATE * 2
    step = piece - OVERLAP_S * SAMPLE_RATE * 2
    out = [pcm[start : start + piece] for start in range(0, max(len(pcm) - 1, 1), step)]
    # A final slice lying entirely inside the previous piece's overlap adds nothing.
    while len(out) > 1 and len(out[-1]) <= OVERLAP_S * SAMPLE_RATE * 2:
        out.pop()
    return out


def _multipart(fields: dict[str, str], wav: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = b""
    for name, value in fields.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="take.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + wav + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def transcribe(pcm: bytes, take_id: str, socket_url: str, token: str) -> str:
    """Batch-transcribe `pcm` (16 kHz mono PCM16) and return the joined text."""
    url = f"{http_base(socket_url)}/api/voice/takes/{take_id}/audio"
    text = ""
    for piece in pieces(pcm):
        fields = {"previous_text": text[-PREVIOUS_TEXT_CHARS:]} if text else {}
        body, content_type = _multipart(fields, wav_header(len(piece)) + piece)
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            piece_text = json.loads(response.read())["text"].strip()
        text = f"{text} {piece_text}".strip()
    return text
