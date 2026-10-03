"""Capture gain: a fixed dB boost or cut on PCM16 samples, saturating at full scale."""

from __future__ import annotations

import sys
from array import array

# Beyond this the setting is a typo, not a microphone correction.
MAX_GAIN_DB = 30.0

_FULL_SCALE_MAX = 32767
_FULL_SCALE_MIN = -32768


def apply_gain(pcm: bytes, gain_db: float) -> bytes:
    """Scale little-endian PCM16 by `gain_db`, clamping instead of wrapping."""
    if gain_db == 0:
        return pcm
    factor = 10 ** (gain_db / 20)
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    scaled = array(
        "h",
        (max(_FULL_SCALE_MIN, min(_FULL_SCALE_MAX, round(s * factor))) for s in samples),
    )
    if sys.byteorder == "big":
        scaled.byteswap()
    return scaled.tobytes()
