"""How this client names itself and its microphone on `hello`."""

from __future__ import annotations

import re
import socket
import subprocess
from typing import Optional

_DESCRIPTION = re.compile(r'node\.description = "(.*)"')


def device_name() -> str:
    return socket.gethostname()


def parse_mic_description(inspect_output: str) -> Optional[str]:
    """The `node.description` line of `wpctl inspect` output."""
    match = _DESCRIPTION.search(inspect_output)
    return match.group(1) if match else None


def default_mic() -> Optional[str]:
    """Description of PipeWire's default source, or None when it cannot be read."""
    try:
        result = subprocess.run(
            ["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_mic_description(result.stdout) if result.returncode == 0 else None
