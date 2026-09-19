#!/usr/bin/env python3
"""pai-stt daemon.

Runs as a systemd user service, listens on a Unix socket for commands from
the CLI and the GNOME Shell keyboard shortcut. Commands: START, STOP,
STATUS, TOGGLE.

Captures the microphone with pw-record and streams it to the PAI Cloud voice
socket while a recording is open; emits DBus signals for the GNOME extension.
`_on_transcript` folds each `transcript` down frame into the take's
assembled text and delivers it to the clipboard and to a file — see
`voice_client` for the frame this is wired to and the composition rule it
implements.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket as socket_module
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from pai_stt.clipboard import clipboard_payload, copy_text
from pai_stt.paths import CONFIG_FILE, OUTPUT_DIR, RESULT_SYMLINK, ensure_output_dir
from pai_stt.voice_client import VoiceSocketCallbacks, VoiceSocketClient

try:
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method
    from dbus_next.service import signal as dbus_signal

    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False

# 16kHz PCM16 mono is what the voice socket's uplink expects.
SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100ms of audio at 16kHz 16-bit mono
SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "pai-stt.sock"
TOKEN_ENV_VAR = "PAI_STT_TOKEN"

SOUND_START = Path("/usr/share/sounds/freedesktop/stereo/device-added.oga")
SOUND_STOP = Path("/usr/share/sounds/freedesktop/stereo/message.oga")
SOUND_DONE = Path("/usr/share/sounds/freedesktop/stereo/complete.oga")
SOUND_ERROR = Path("/usr/share/sounds/freedesktop/stereo/dialog-warning.oga")

DBUS_NAME = "com.github.frederikb.PaiStt"
DBUS_PATH = "/com/github/frederikb/PaiStt"

logger = logging.getLogger("pai_stt")


def setup_logging(level: str) -> None:
    """Configure logging with the given level name."""
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.setLevel(level_map.get(level.lower(), logging.INFO))
    logger.addHandler(handler)


def load_config() -> dict[str, Any]:
    """Load configuration from config.yaml. Fails if not found."""
    if not CONFIG_FILE.exists():
        print(f"[CONFIG] ERROR: Config not found: {CONFIG_FILE}", flush=True)
        print("[CONFIG] Run 'pai-stt setup' first to create config.", flush=True)
        sys.exit(1)
    try:
        with open(CONFIG_FILE) as f:
            config: dict[str, Any] = yaml.safe_load(f)
        logger.info(f"Config loaded from {CONFIG_FILE}")
        return config
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        sys.exit(1)


class State(Enum):
    """Daemon state machine."""

    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"


if DBUS_AVAILABLE:

    class PaiSttDBusInterface(ServiceInterface):  # type: ignore[misc]
        """DBus interface for status updates to the GNOME extension.

        Signals carry truncated text for the panel; GetStatus returns the
        full text on demand for the popup.
        """

        def __init__(self, text_provider: "Any", copier: "Any") -> None:
            super().__init__(DBUS_NAME)
            self._state = "idle"
            self._signal_text = ""
            self._text_provider = text_provider
            self._copier = copier

        # dbus_next reads its DBus signature straight off this string return
        # annotation ("ss" = two strings, "b" = one boolean) rather than a
        # real Python type, which is why ruff and mypy both misread it as an
        # undefined name.
        @dbus_signal()
        def StateChanged(self) -> "ss":  # type: ignore[name-defined]  # noqa: N802, F821
            return [self._state, self._signal_text]

        @method()
        def GetStatus(self) -> "ss":  # type: ignore[name-defined]  # noqa: N802, F821
            return [self._state, self._text_provider()]

        @method()
        async def CopyToClipboard(self) -> "b":  # type: ignore[name-defined]  # noqa: N802, F821
            return await self._copier()

        def emit_state(self, state: str, text: str = "") -> None:
            self._state = state
            self._signal_text = text
            self.StateChanged()


class PaiSttDaemon:
    """Owns the recording state machine, the voice socket and DBus."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.state = State.IDLE
        self.config = config
        self.token: str = ""
        self.pw_record_proc: Optional[asyncio.subprocess.Process] = None
        self.pump_task: Optional[asyncio.Task[None]] = None
        self.voice: Optional[VoiceSocketClient] = None
        self.shutdown_event = asyncio.Event()
        self.current_output_file: Optional[Path] = None
        self.current_text: str = ""
        self._committed_segments: dict[int, str] = {}
        self._partial_text: str = ""
        self.dbus_interface: Optional[Any] = None
        self.dbus_bus: Optional[Any] = None

    def load_token(self) -> bool:
        """Load the PAI Cloud bearer token from the environment."""
        self.token = os.environ.get(TOKEN_ENV_VAR, "")
        if self.token:
            return True
        logger.error(f"{TOKEN_ENV_VAR} environment variable not set")
        return False

    async def setup_dbus(self) -> None:
        """Set up the DBus service the GNOME extension talks to."""
        if not DBUS_AVAILABLE:
            logger.info("DBus not available (dbus-next not installed)")
            return
        try:
            self.dbus_bus = await MessageBus().connect()
            self.dbus_interface = PaiSttDBusInterface(
                lambda: self.current_text, self._copy_current_text
            )
            self.dbus_bus.export(DBUS_PATH, self.dbus_interface)
            await self.dbus_bus.request_name(DBUS_NAME)
            logger.info(f"DBus service registered: {DBUS_NAME}")
        except Exception as e:
            logger.warning(f"DBus setup failed (extension won't work): {e}")
            self.dbus_interface = None

    def emit_state(self, state: str, text: str = "") -> None:
        if self.dbus_interface:
            self.dbus_interface.emit_state(state, text)

    def play_sound(self, sound_file: Path) -> None:
        """Play a sound file without blocking the event loop."""
        import subprocess

        if sound_file.exists():
            try:
                subprocess.Popen(
                    ["pw-play", str(sound_file)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.debug(f"Sound play failed: {e}")

    async def copy_to_clipboard(self, text: str) -> bool:
        try:
            await copy_text(clipboard_payload(text), self.dbus_bus)
            return True
        except Exception as e:
            logger.error(f"Clipboard delivery failed: {e}")
            return False

    async def _copy_current_text(self) -> bool:
        if not self.current_text:
            logger.info("Copy requested with no transcription available")
            return False
        return await self.copy_to_clipboard(self.current_text)

    def _write_result_file(self, text: str) -> None:
        if self.current_output_file is None:
            return
        self.current_output_file.write_text(text)

    def _voice_callbacks(self) -> VoiceSocketCallbacks:
        return VoiceSocketCallbacks(
            on_ready=lambda msg: logger.info(f"Voice socket ready: {msg}"),
            on_ack=lambda through_seq: logger.debug(f"ack through_seq={through_seq}"),
            on_state=lambda msg: logger.debug(f"state: {msg}"),
            on_notice=self._on_notice,
            on_clear=lambda: logger.debug("clear"),
            on_audio=lambda ref, pcm: logger.warning(
                "Received downlink audio on a no-downlink transport"
            ),
            on_transcript=self._on_transcript,
        )

    def _on_notice(self, msg: dict[str, Any]) -> None:
        severity = msg.get("severity", "info")
        text = msg.get("text", "")
        logger.log(
            logging.ERROR if severity == "error" else logging.INFO,
            f"notice[{msg.get('code')}]: {text}",
        )

    def _on_transcript(self, text: str, is_final: bool, seq: int) -> None:
        """Fold one `transcript` frame into the take's assembled text.

        Renders every `is_final` segment seen so far, in `seq` order,
        followed by the latest non-final one — the composition rule the
        protocol document specifies, mirroring the backend's own
        `TakeLedger.assembled_text`.
        """
        if is_final:
            self._committed_segments[seq] = text
            self._partial_text = ""
        else:
            self._partial_text = text
        self.current_text = self._assembled_text()
        self._write_result_file(self.current_text)
        self.emit_state("recording", self.current_text[-500:])

    def _assembled_text(self) -> str:
        parts = [self._committed_segments[seq] for seq in sorted(self._committed_segments)]
        if self._partial_text:
            parts.append(self._partial_text)
        return " ".join(part for part in parts if part)

    async def start_recording(self) -> tuple[bool, str]:
        if self.state != State.IDLE:
            return False, f"Cannot start: state is {self.state.value}"
        if not self.token and not self.load_token():
            return False, f"{TOKEN_ENV_VAR} not set"

        self.state = State.RECORDING
        self.play_sound(SOUND_START)
        self.emit_state("recording", "")
        self.current_text = ""
        self._committed_segments = {}
        self._partial_text = ""
        logger.info("Starting recording session")

        ensure_output_dir()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.current_output_file = OUTPUT_DIR / f"pai-stt-{timestamp}.txt"
        self.current_output_file.touch()
        RESULT_SYMLINK.unlink(missing_ok=True)
        RESULT_SYMLINK.symlink_to(self.current_output_file)

        url = self.config["pai_cloud"]["socket_url"]
        self.voice = VoiceSocketClient(url, self.token, self._voice_callbacks())
        try:
            await self.voice.connect()
        except Exception as e:
            logger.error(f"Voice socket connect failed: {e}")
            self.play_sound(SOUND_ERROR)
            self.emit_state("error", "")
            self.state = State.IDLE
            return False, f"Voice socket connect failed: {e}"

        try:
            self.pw_record_proc = await asyncio.create_subprocess_exec(
                "pw-record",
                "--rate",
                str(SAMPLE_RATE),
                "--format",
                "s16",
                "--channels",
                "1",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info(f"pw-record started (PID {self.pw_record_proc.pid})")
        except Exception as e:
            logger.error(f"Failed to start pw-record: {e}")
            await self.voice.close()
            self.play_sound(SOUND_ERROR)
            self.emit_state("error", "")
            self.state = State.IDLE
            return False, f"Failed to start audio capture: {e}"

        await self.voice.open_gate(reason="button")
        self.pump_task = asyncio.create_task(self._pump_audio())
        return True, "Recording started"

    async def _pump_audio(self) -> None:
        """Read pw-record's stdout and forward it as uplink audio frames."""
        assert self.pw_record_proc is not None
        assert self.pw_record_proc.stdout is not None
        assert self.voice is not None
        sample_offset = 0
        try:
            while self.state == State.RECORDING:
                chunk = await self.pw_record_proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                await self.voice.send_audio(sample_offset, chunk)
                sample_offset += len(chunk) // 2
        except Exception as e:
            logger.error(f"Audio pump failed: {e}")

    async def _terminate_pw_record(self) -> None:
        if not self.pw_record_proc:
            return
        try:
            self.pw_record_proc.terminate()
            await asyncio.wait_for(self.pw_record_proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            self.pw_record_proc.kill()
            try:
                await asyncio.wait_for(self.pw_record_proc.wait(), timeout=1)
            except asyncio.TimeoutError:
                logger.error("pw-record didn't respond to SIGKILL")
        except Exception as e:
            logger.error(f"Error terminating pw-record: {e}")
        finally:
            self.pw_record_proc = None

    async def stop_recording(self) -> tuple[bool, str]:
        if self.state != State.RECORDING:
            return False, f"Cannot stop: state is {self.state.value}"

        self.state = State.TRANSCRIBING
        self.play_sound(SOUND_STOP)
        self.emit_state("transcribing", "")
        logger.info("Stopping recording session")

        await self._terminate_pw_record()
        if self.pump_task:
            await self.pump_task
            self.pump_task = None

        if self.voice:
            try:
                await self.voice.close_gate(reason="button")
                await self.voice.bye(reason="stop")
            except Exception as e:
                logger.warning(f"Voice socket shutdown was not clean: {e}")
            await self.voice.close()
            self.voice = None

        if self.current_text:
            self.play_sound(SOUND_DONE)
            self.emit_state("done", self.current_text[-500:])
        else:
            self.emit_state("partial", "")
        self.state = State.IDLE
        return True, "Recording stopped"

    async def toggle_recording(self) -> tuple[bool, str]:
        if self.state == State.IDLE:
            return await self.start_recording()
        if self.state == State.RECORDING:
            return await self.stop_recording()
        return False, f"Cannot toggle: state is {self.state.value}"

    def status(self) -> str:
        return self.state.value

    async def handle_command(self, command: str) -> str:
        command = command.strip().upper()
        if command == "START":
            ok, msg = await self.start_recording()
        elif command == "STOP":
            ok, msg = await self.stop_recording()
        elif command == "TOGGLE":
            ok, msg = await self.toggle_recording()
        elif command == "STATUS":
            return f"OK {self.status()}"
        else:
            return f"ERROR Unknown command: {command}"
        return f"{'OK' if ok else 'ERROR'} {msg}"

    async def shutdown(self) -> None:
        logger.info("Shutting down")
        if self.state == State.RECORDING:
            await self.stop_recording()
        if self.pw_record_proc:
            self.pw_record_proc.kill()
        self.shutdown_event.set()


async def _serve_command(daemon: PaiSttDaemon, reader: Any, writer: Any) -> None:
    data = await reader.read(1024)
    command = data.decode().strip()
    logger.info(f"Received command: {command}")
    reply = await daemon.handle_command(command)
    writer.write(f"{reply}\n".encode())
    await writer.drain()
    writer.close()


async def run_daemon() -> None:
    config = load_config()
    setup_logging(config.get("log_level", "info"))
    logger.info("pai-stt daemon starting")

    daemon = PaiSttDaemon(config)
    daemon.load_token()
    await daemon.setup_dbus()

    SOCKET_PATH.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(
        lambda r, w: _serve_command(daemon, r, w), path=str(SOCKET_PATH)
    )
    logger.info(f"Listening on {SOCKET_PATH}")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.shutdown()))

    async with server:
        await daemon.shutdown_event.wait()
    server.close()
    SOCKET_PATH.unlink(missing_ok=True)


def send_command(command: str, timeout: float = 15) -> str:
    """Send a command to a running daemon over the Unix socket. CLI-side helper."""
    with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(command.encode())
        return sock.recv(4096).decode().strip()


def main() -> None:
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()
