"""Server-owned takes over REST: read, seal, and upload audio into a take.

A batch take lives on the backend as a take resource (the protocol document's
"Takes"). The socket carries its audio while connected; whatever the server
reports `missing` after the seal is uploaded here from the local recording,
and the finished text is read back from the resource. Every call is retried
on what the protocol calls transient (transport errors, 5xx, 408, 429) and
never on any other 4xx.
"""

from __future__ import annotations

import functools
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pai_stt.batch import http_base, multipart
from pai_stt.recordings import SAMPLE_RATE, wav_header

logger = logging.getLogger("pai_stt.takes")

# The route accepts pieces of at most 600 s; smaller ones keep a retry cheap.
UPLOAD_PIECE_S = 120
REQUEST_TIMEOUT_S = 60
RETRY_FIRST_S = 2.0
RETRY_MAX_S = 60.0
TRANSIENT_STATUS = {408, 429}


class PermanentTakeError(Exception):
    """A 4xx the protocol says not to retry; the take cannot proceed this way."""


@dataclass(frozen=True)
class TakeView:
    """The parts of the take resource (`VoiceTake`) this client acts on."""

    state: str
    missing: list[tuple[int, int]]
    text: Optional[str]
    ended: Optional[str]
    failure: Optional[str]

    @classmethod
    def parse(cls, body: dict[str, Any]) -> TakeView:
        failure = body.get("failure")
        return cls(
            state=body["state"],
            missing=[(int(s), int(e)) for s, e in body.get("missing") or []],
            text=body.get("text"),
            ended=body.get("ended"),
            failure=failure.get("code") if isinstance(failure, dict) else None,
        )


class TakeClient:
    """REST access to one deployment's take routes, authenticated per request."""

    def __init__(
        self,
        socket_url: str,
        token: Callable[[], str],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = f"{http_base(socket_url)}/api/voice/takes"
        self._token = token
        self._sleep = sleep

    def _request(
        self, path: str, data: Optional[bytes] = None, content_type: Optional[str] = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            f"{self._base}/{path}", data=data, headers=headers,
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            body: dict[str, Any] = json.loads(response.read())
            return body

    def _retrying(self, what: str, call: Callable[[], dict[str, Any]], deadline: float) -> Any:
        """Run `call` until it succeeds, a permanent error, or `deadline` (monotonic) passes."""
        delay = RETRY_FIRST_S
        while True:
            try:
                return call()
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 and e.code not in TRANSIENT_STATUS:
                    detail = e.read()[:300].decode(errors="replace")
                    raise PermanentTakeError(f"{what}: HTTP {e.code} {detail}") from e
                error: Exception = e
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                error = e
            if time.monotonic() + delay > deadline:
                raise ConnectionError(f"{what} kept failing: {error}") from error
            logger.warning(f"{what} failed, retrying in {delay:.0f}s: {error}")
            self._sleep(delay)
            delay = min(delay * 2, RETRY_MAX_S)

    def get(self, take_id: str, deadline: float) -> TakeView:
        return TakeView.parse(
            self._retrying(f"reading take {take_id[:8]}", lambda: self._request(take_id), deadline)
        )

    def seal(
        self, take_id: str, captured_samples: int, device: str, deadline: float
    ) -> TakeView:
        """Seal (and create, if the socket never reached the server) a standalone batch take."""
        seal = {"mode": "batch", "captured_samples": captured_samples, "withheld": []}
        body = json.dumps({**seal, "device": device}).encode()
        return TakeView.parse(
            self._retrying(
                f"sealing take {take_id[:8]}",
                lambda: self._request(f"{take_id}/seal", body, "application/json"),
                deadline,
            )
        )

    def upload(self, take_id: str, pcm: bytes, start: int, deadline: float) -> None:
        """Store samples `pcm` at take-absolute `start`, in pieces the route accepts."""
        piece = UPLOAD_PIECE_S * SAMPLE_RATE
        samples = len(pcm) // 2
        for offset in range(0, samples, piece):
            chunk = pcm[offset * 2 : (offset + piece) * 2]
            begin = start + offset
            end = begin + len(chunk) // 2
            fields = {"into_take": "true", "start_sample": str(begin), "end_sample": str(end)}
            body, content_type = multipart(fields, wav_header(len(chunk)) + chunk)
            self._retrying(
                f"uploading [{begin}, {end}) into take {take_id[:8]}",
                functools.partial(self._request, f"{take_id}/audio", body, content_type),
                deadline,
            )
