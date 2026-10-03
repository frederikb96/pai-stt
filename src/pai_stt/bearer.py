"""Bearer token lookup, shared by the daemon and the CLI."""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

COMMAND_OPTION = "token_command"
COMMAND_TIMEOUT_S = 10


class TokenError(Exception):
    """The bearer token could not be resolved."""


def resolve_token(config: dict[str, Any]) -> str:
    """Run the configured `token_command` and return its stdout, which must be the token alone.

    The token exists only in the returned value: it is fetched per use, never cached on
    disk, and nothing here logs it. Failure messages name the command and its exit status,
    never its stdout.
    """
    command = str(config.get(COMMAND_OPTION) or "").strip()
    if not command:
        raise TokenError(f"{COMMAND_OPTION} is not configured; see config.example.yaml")
    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        raise TokenError(f"{COMMAND_OPTION} '{command}' failed: {e}") from e
    if result.returncode != 0:
        raise TokenError(
            f"{COMMAND_OPTION} '{command}' exited {result.returncode}: {result.stderr.strip()}"
        )
    token = result.stdout.strip()
    if not token:
        raise TokenError(f"{COMMAND_OPTION} '{command}' printed nothing")
    return token
