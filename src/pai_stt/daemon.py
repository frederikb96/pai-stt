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
import subprocess
import sys
import uuid
from datetime import datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from pai_stt.bearer import TokenError, resolve_token
from pai_stt.clipboard import clipboard_payload, copy_text
from pai_stt.device import default_mic, device_name
from pai_stt.gain import MAX_GAIN_DB, apply_gain
from pai_stt.paths import (
    CONFIG_FILE,
    OUTPUT_DIR,
    RECORDINGS_DIR,
    RESULT_SYMLINK,
    ensure_output_dir,
)
from pai_stt.recordings import RecordingWriter
from pai_stt.silence_gate import Frame, SilenceGate
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

# Capture keeps running this long after stop is pressed so the last syllable
# still in the pipeline is not cut off.
STOP_TAIL_S = 0.3

# Config keys that have no built-in default; see config.example.yaml.
REQUIRED_CONFIG = (
    ("pai_cloud", "socket_url"),
    ("token_command",),
    ("transcription_timeout",),
    ("silence_gate",),
    ("capture", "gain_db"),
)

# What stop adds to `transcription_timeout` in the worst case: the tail, the pw-record
# terminate/kill waits, and the gate close, bye and socket close around the receipt wait.
STOP_OVERHEAD_S = 15.0
DEFAULT_COMMAND_TIMEOUT_S = 15.0

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


@lru_cache(maxsize=1)
def _pw_record_supports_raw() -> bool:
    """Whether this `pw-record` has `--raw` (PipeWire 1.3.81 and newer)."""
    try:
        result = subprocess.run(["pw-record", "--help"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--raw" in result.stdout + result.stderr


def capture_command() -> list[str]:
    """The `pw-record` command that writes bare PCM16 samples to stdout.

    Newer PipeWire wraps stdout in an AU container header unless `--raw` is passed; the pump
    forwards every byte as audio, so the header would open each take with a click. Older
    versions write bare samples to `-` already and do not know the flag.
    """
    command = [
        "pw-record",
        "--rate",
        str(SAMPLE_RATE),
        "--format",
        "s16",
        "--channels",
        "1",
    ]
    if _pw_record_supports_raw():
        command.append("--raw")
    return [*command, "-"]


def load_config() -> dict[str, Any]:
    """Load configuration from config.yaml. Fails if not found."""
    if not CONFIG_FILE.exists():
        print(f"[CONFIG] ERROR: Config not found: {CONFIG_FILE}", flush=True)
        print("[CONFIG] Run 'pai-stt setup' first to create config.", flush=True)
        sys.exit(1)
    try:
        with open(CONFIG_FILE) as f:
            config: dict[str, Any] = yaml.safe_load(f)
        for path in REQUIRED_CONFIG:
            node: Any = config
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    print(
                        f"[CONFIG] ERROR: missing {'.'.join(path)} in {CONFIG_FILE}; "
                        "see config.example.yaml",
                        flush=True,
                    )
                    sys.exit(1)
                node = node[key]
        gain_db = config["capture"]["gain_db"]
        if isinstance(gain_db, bool) or not isinstance(gain_db, (int, float)):
            print(f"[CONFIG] ERROR: capture.gain_db must be a number in {CONFIG_FILE}", flush=True)
            sys.exit(1)
        if abs(gain_db) > MAX_GAIN_DB:
            print(
                f"[CONFIG] ERROR: capture.gain_db must be within +/-{MAX_GAIN_DB:g} dB "
                f"in {CONFIG_FILE}",
                flush=True,
            )
            sys.exit(1)
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
        self.pw_record_proc: Optional[asyncio.subprocess.Process] = None
        self.pump_task: Optional[asyncio.Task[None]] = None
        self.voice: Optional[VoiceSocketClient] = None
        self.shutdown_event = asyncio.Event()
        self.current_output_file: Optional[Path] = None
        self.current_text: str = ""
        self._committed_segments: dict[int, tuple[Optional[int], str]] = {}
        self._partial_text: str = ""
        self._take_id: Optional[str] = None
        self._writer: Optional[RecordingWriter] = None
        self._gate: Optional[SilenceGate] = None
        self._uplink_ok = True
        self._uplink_ready = False
        self._connect_task: Optional[asyncio.Task[None]] = None
        self._pending: list[Frame] = []
        self._gain_db = 0.0
        self.dbus_interface: Optional[Any] = None
        self.dbus_bus: Optional[Any] = None

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
            on_closed=self._on_closed,
        )

    def _on_closed(self) -> None:
        logger.warning("Voice socket closed")
        self._uplink_ok = False

    def _on_notice(self, msg: dict[str, Any]) -> None:
        severity = msg.get("severity", "info")
        text = msg.get("text", "")
        logger.log(
            logging.ERROR if severity == "error" else logging.INFO,
            f"notice[{msg.get('code')}]: {text}",
        )

    def _on_transcript(
        self,
        text: str,
        is_final: bool,
        seq: int,
        end_sample: Optional[int] = None,
        take_id: Optional[str] = None,
    ) -> None:
        """Fold one `transcript` frame into the take's assembled text.

        Renders every `is_final` segment seen so far, ordered by `end_sample`
        (by `seq` for a frame that carries none), followed by the latest
        non-final one — the composition rule the protocol document
        specifies, mirroring the backend's own `TakeLedger.assembled_text`.
        Frames for another take are dropped.
        """
        if take_id is not None and self._take_id is not None and take_id != self._take_id:
            return
        if is_final:
            self._committed_segments[seq] = (end_sample, text)
            self._partial_text = ""
        else:
            self._partial_text = text
        self.current_text = self._assembled_text()
        self._write_result_file(self.current_text)
        self.emit_state(self.state.value, self.current_text[-500:])

    def _assembled_text(self) -> str:
        ordered = sorted(
            self._committed_segments.items(),
            key=lambda item: (item[1][0] if item[1][0] is not None else item[0], item[0]),
        )
        parts = [text for _, (_, text) in ordered]
        if self._partial_text:
            parts.append(self._partial_text)
        return " ".join(part for part in parts if part)

    async def start_recording(self) -> tuple[bool, str]:
        if self.state != State.IDLE:
            return False, f"Cannot start: state is {self.state.value}"
        # Claim the state before the first await so a second START cannot slip in.
        self.state = State.RECORDING
        try:
            token = await asyncio.to_thread(resolve_token, self.config)
        except TokenError as e:
            logger.error(f"No bearer token: {e}")
            self.play_sound(SOUND_ERROR)
            self.state = State.IDLE
            return False, f"No bearer token: {e}"

        self.play_sound(SOUND_START)
        self.emit_state("recording", "")
        self.current_text = ""
        self._committed_segments = {}
        self._partial_text = ""
        self._take_id = str(uuid.uuid4())
        logger.info("Starting recording session")

        ensure_output_dir()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.current_output_file = OUTPUT_DIR / f"pai-stt-{timestamp}.txt"
        self.current_output_file.touch()
        RESULT_SYMLINK.unlink(missing_ok=True)
        RESULT_SYMLINK.symlink_to(self.current_output_file)

        gate_config = self.config["silence_gate"]
        self._gate = SilenceGate(
            enabled=gate_config["enabled"],
            mode=gate_config["mode"],
            manual_threshold_db=gate_config["manual_threshold_db"],
        )
        self._gain_db = float(self.config["capture"]["gain_db"])
        self._uplink_ok = True
        self._uplink_ready = False
        self._pending = []
        self.voice = VoiceSocketClient(
            self.config["pai_cloud"]["socket_url"],
            token,
            self._voice_callbacks(),
            device_name=device_name(),
            mic=default_mic(),
            silence_gate=gate_config["enabled"],
        )

        # Capture and the local recording start first; the connection is made in
        # parallel and the frames captured meanwhile are flushed once it is ready.
        try:
            self.pw_record_proc = await asyncio.create_subprocess_exec(
                *capture_command(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info(f"pw-record started (PID {self.pw_record_proc.pid})")
        except Exception as e:
            logger.error(f"Failed to start pw-record: {e}")
            self.play_sound(SOUND_ERROR)
            self.emit_state("error", "")
            self.voice = None
            self.state = State.IDLE
            return False, f"Failed to start audio capture: {e}"

        self._writer = RecordingWriter(RECORDINGS_DIR, self._take_id)
        self._connect_task = asyncio.create_task(self._connect_uplink(self.voice, self._take_id))
        self.pump_task = asyncio.create_task(self._pump_audio())
        return True, "Recording started"

    async def _connect_uplink(self, voice: VoiceSocketClient, take_id: str) -> None:
        """Connect and open the gate; on failure the local recording carries on alone."""
        try:
            await voice.connect()
            await voice.open_gate(reason="button", take_id=take_id)
        except Exception as e:
            logger.error(f"Voice socket connect failed, recording continues locally: {e}")
            self._uplink_ok = False
            self._pending = []
            self.play_sound(SOUND_ERROR)
            try:
                await voice.close()
            except Exception as close_error:
                logger.debug(f"Closing the failed voice socket: {close_error}")
            return
        self._uplink_ready = True

    async def _drain_pending(self) -> None:
        """Send the frames captured so far, in order, once the uplink is ready."""
        if not self._uplink_ok:
            self._pending = []
            return
        if not self._uplink_ready:
            return
        while self._pending:
            await self._feed(self._pending.pop(0))

    async def _pump_audio(self) -> None:
        """Read pw-record's stdout to EOF; record every chunk, send what the gate lets through."""
        assert self.pw_record_proc is not None
        assert self.pw_record_proc.stdout is not None
        stdout = self.pw_record_proc.stdout
        sample_offset = 0
        try:
            while True:
                last = False
                try:
                    chunk = await stdout.readexactly(CHUNK_BYTES)
                except asyncio.IncompleteReadError as e:
                    chunk = e.partial[: len(e.partial) // 2 * 2]
                    last = True
                if chunk:
                    # The local recording keeps what the microphone delivered; the gain
                    # applies to what the gate measures and the backend receives.
                    if self._writer:
                        self._writer.append(chunk)
                    frame = Frame(sample_offset, apply_gain(chunk, self._gain_db))
                    sample_offset = frame.end
                    self._pending.append(frame)
                    await self._drain_pending()
                if last:
                    break
        except Exception as e:
            logger.error(f"Audio pump failed: {e}")

    async def _feed(self, frame: Frame) -> None:
        """Pass one captured frame through the gate and onto the wire."""
        assert self._gate is not None and self.voice is not None
        was_withholding = self._gate.is_withholding
        result = self._gate.push(frame, self.voice.silence_allowed)
        if result.silence_at is not None:
            logger.info(f"gate withhold at_sample={result.silence_at}")
        elif was_withholding and result.send:
            logger.info(f"gate resume preroll_frames={len(result.send)}")
        await self._send(result.send, result.silence_at)

    async def _send(self, frames: list[Frame], silence_at: Optional[int] = None) -> None:
        if not self._uplink_ok or self.voice is None:
            return
        try:
            for frame in frames:
                await self.voice.send_audio(frame.offset, frame.pcm)
            if silence_at is not None:
                await self.voice.send_silence(silence_at)
        except Exception as e:
            logger.error(f"Uplink failed, recording continues locally: {e}")
            self._uplink_ok = False

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

        # Tail, drain, close the gate, wait for the receipt: capture runs a
        # little past the button press, the pump reads pw-record to EOF so no
        # captured sample is dropped, and `take_done` says nothing more is coming.
        await asyncio.sleep(STOP_TAIL_S)
        await self._terminate_pw_record()
        if self.pump_task:
            await self.pump_task
            self.pump_task = None

        if self._connect_task:
            await self._connect_task
            self._connect_task = None
            await self._drain_pending()

        take_id = self._take_id
        ended: Optional[str] = None
        if self.voice and take_id and self._uplink_ready:
            if self._gate:
                await self._send(self._gate.stop_flush())
            try:
                await self.voice.close_gate(reason="button")
                done = await self.voice.wait_take_done(
                    take_id, float(self.config["transcription_timeout"])
                )
                ended = done.ended if done else None
                logger.info(f"take_done ended={ended}")
                await self.voice.bye(reason="stop")
            except Exception as e:
                logger.warning(f"Voice socket shutdown was not clean: {e}")
            await self.voice.close()
        self.voice = None
        self._uplink_ready = False

        complete = ended is not None and ended != "unavailable"
        if self._writer:
            self._writer.finish(self.current_text, complete)
            self._writer = None

        if self.current_text:
            await self.copy_to_clipboard(self.current_text)
        if self.current_text and complete:
            self.play_sound(SOUND_DONE)
            self.emit_state("done", self.current_text[-500:])
        else:
            self.emit_state("partial", self.current_text[-500:])
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
    try:
        writer.write(f"{reply}\n".encode())
        await writer.drain()
    except ConnectionError:
        logger.warning("Command client went away before the reply was written")
    finally:
        writer.close()


async def run_daemon() -> None:
    config = load_config()
    setup_logging(config.get("log_level", "info"))
    logger.info("pai-stt daemon starting")

    daemon = PaiSttDaemon(config)
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


def command_timeout(command: str, load: Callable[[], dict[str, Any]]) -> float:
    """Seconds a CLI waits for the daemon's reply: a stop outlasts the transcription wait.

    `load` reads the config, so only a command that can stop a take needs one.
    """
    if command.lower() in ("stop", "toggle"):
        return float(load()["transcription_timeout"]) + STOP_OVERHEAD_S
    return DEFAULT_COMMAND_TIMEOUT_S


def send_command(command: str, timeout: float = DEFAULT_COMMAND_TIMEOUT_S) -> str:
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
