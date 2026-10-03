#!/usr/bin/env python3
"""pai-stt CLI.

Usage:
    pai-stt setup             Install systemd service and create config
    pai-stt teardown          Remove systemd service
    pai-stt install-extension Install GNOME Shell extension
    pai-stt start             Start recording
    pai-stt stop               Stop recording
    pai-stt toggle             Toggle recording (default)
    pai-stt status             Check daemon status
    pai-stt recordings         List past recordings (newest first)
    pai-stt transcript <id|last>     Print a past recording's transcript
    pai-stt retranscribe <id|last>   Transcribe a past recording again via the backend
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from pai_stt import batch, recordings
from pai_stt.daemon import TOKEN_ENV_VAR, load_config, send_command
from pai_stt.paths import CONFIG_DIR, CONFIG_FILE, RECORDINGS_DIR

SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
SERVICE_FILE = SYSTEMD_DIR / "pai-stt.service"
EXTENSION_UUID = "pai-stt@frederikb.github.com"

DEFAULT_CONFIG = """# pai-stt Configuration

log_level: info

pai_cloud:
  # Full wss:// URL of the PAI Cloud voice socket.
  socket_url: wss://your-pai-cloud-host/api/voice/socket

# Max seconds to wait for the backend's receipt that a take is finished
transcription_timeout: 30

# Stop sending audio after a sustained quiet stretch (capture and the local
# recording continue). mode: auto adapts to the room; manual uses
# manual_threshold_db (dBFS, -70 to -20).
silence_gate:
  enabled: true
  mode: auto
  manual_threshold_db: -45
"""


def get_service_content() -> str:
    """Generate systemd service file content with the correct Python path."""
    python_path = sys.executable
    return f"""[Unit]
Description=pai-stt - Linux dictation client for PAI Cloud
After=graphical-session.target
PartOf=graphical-session.target
BindsTo=graphical-session.target

[Service]
Type=simple
ExecStart={python_path} -m pai_stt.daemon
Restart=always
RestartSec=3
Environment="XDG_RUNTIME_DIR=%t"
PassEnvironment={TOKEN_ENV_VAR}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pai-stt

[Install]
WantedBy=graphical-session.target
"""


def run_systemctl(*args: str) -> tuple[bool, str]:
    """Run systemctl --user and return success status and combined output."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except FileNotFoundError:
        return False, "systemctl not found"


def setup() -> int:
    """Install systemd service and create config directory."""
    print("Setting up pai-stt...")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  Config directory: {CONFIG_DIR}")

    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG)
        print(f"  Created config: {CONFIG_FILE}")
    else:
        print(f"  Config exists: {CONFIG_FILE}")

    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_FILE.write_text(get_service_content())
    print(f"  Service file: {SERVICE_FILE}")

    ok, _ = run_systemctl("daemon-reload")
    if not ok:
        print("  ERROR: Failed to reload systemd daemon")
        return 1
    print("  Reloaded systemd daemon")

    subprocess.run(
        [
            "systemctl",
            "--user",
            "import-environment",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
            TOKEN_ENV_VAR,
        ],
        capture_output=True,
    )
    print("  Imported Wayland environment")

    ok, _ = run_systemctl("enable", "pai-stt")
    if not ok:
        print("  ERROR: Failed to enable service")
        return 1
    print("  Enabled service")

    ok, output = run_systemctl("start", "pai-stt")
    if not ok:
        print(f"  ERROR: Failed to start service: {output}")
        return 1
    print("  Started service")

    print("\nSetup complete! Edit the config's socket_url, then 'pai-stt toggle' to record.")
    print("Logs: journalctl --user -u pai-stt -f")
    return 0


def install_extension() -> int:
    """Install the GNOME Shell extension."""
    print("Installing GNOME Shell extension...")

    ext_dir = Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / EXTENSION_UUID
    ext_dir.mkdir(parents=True, exist_ok=True)

    package_dir = Path(__file__).parent.parent.parent
    ext_src = package_dir / "extension"
    if not ext_src.exists():
        ext_src = Path.cwd() / "extension"
    if not ext_src.exists():
        print("  ERROR: Extension source not found")
        print("  Run this command from the pai-stt repository root")
        return 1

    import shutil

    for filename in ("extension.js", "metadata.json", "stylesheet.css", "prefs.js"):
        src = ext_src / filename
        if src.exists():
            shutil.copy(src, ext_dir / filename)
            print(f"  Copied: {filename}")
        else:
            print(f"  WARNING: Missing {filename}")

    schema_src = ext_src / "schemas"
    schema_dst = ext_dir / "schemas"
    if schema_src.exists():
        schema_dst.mkdir(exist_ok=True)
        for schema_file in schema_src.glob("*.xml"):
            shutil.copy(schema_file, schema_dst / schema_file.name)
            print(f"  Copied: schemas/{schema_file.name}")

        try:
            result = subprocess.run(
                ["glib-compile-schemas", str(schema_dst)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"  ERROR: Schema compilation failed: {result.stderr}")
                return 1
            print("  Compiled schemas")
        except FileNotFoundError:
            print("  ERROR: glib-compile-schemas not found")
            print("  Install: sudo apt install libglib2.0-dev-bin")
            return 1

    print(f"\nExtension installed to: {ext_dir}")
    print("\nTo activate:")
    print("  1. Log out and log back in (or restart GNOME Shell on X11: Alt+F2, 'r', Enter)")
    print(f"  2. Enable extension: gnome-extensions enable {EXTENSION_UUID}")
    print(f"  3. Open settings: gnome-extensions prefs {EXTENSION_UUID}")
    return 0


def teardown() -> int:
    """Remove the systemd service."""
    print("Removing pai-stt service...")
    run_systemctl("stop", "pai-stt")
    print("  Stopped service")
    run_systemctl("disable", "pai-stt")
    print("  Disabled service")
    if SERVICE_FILE.exists():
        SERVICE_FILE.unlink()
        print(f"  Removed {SERVICE_FILE}")
    run_systemctl("daemon-reload")
    print("  Reloaded systemd daemon")
    print("\nTeardown complete!")
    print(f"Config preserved at: {CONFIG_DIR}")
    print("To fully uninstall: pipx uninstall pai-stt")
    return 0


def list_recordings(directory: Path) -> int:
    """Print one line per past recording, newest first."""
    for rec in recordings.list_recordings(directory):
        started = datetime.fromisoformat(rec.started_at).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        flag = "" if rec.transcript_complete else " (incomplete)"
        preview = rec.transcript[:60].replace("\n", " ")
        print(f"{rec.id[:8]}  {started}  {rec.duration_ms / 1000:7.1f}s{flag}  {preview}")
    return 0


def print_transcript(directory: Path, ref: str) -> int:
    try:
        rec = recordings.resolve(directory, ref)
    except LookupError as e:
        print(f"ERROR: {e}")
        return 1
    if not rec.transcript:
        print(f"ERROR: recording {rec.id[:8]} has no transcript; try 'pai-stt retranscribe'")
        return 1
    print(rec.transcript)
    return 0


def retranscribe(directory: Path, ref: str, socket_url: str, token: str) -> int:
    """Send a past recording through the backend's batch route and store the result."""
    try:
        rec = recordings.resolve(directory, ref)
    except LookupError as e:
        print(f"ERROR: {e}")
        return 1
    pcm = recordings.read_pcm(directory, rec.id)
    if not pcm:
        print(f"ERROR: recording {rec.id[:8]} holds no audio")
        return 1
    try:
        text = batch.transcribe(pcm, rec.id, socket_url, token)
    except Exception as e:
        print(f"ERROR: transcription failed: {e}")
        return 1
    recordings.store_transcript(directory, rec.id, text, "batch")
    print(text)
    return 0


def main() -> NoReturn:
    """Entry point."""
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "toggle").lower()

    if cmd == "setup":
        sys.exit(setup())
    if cmd == "teardown":
        sys.exit(teardown())
    if cmd == "install-extension":
        sys.exit(install_extension())

    if cmd == "recordings":
        sys.exit(list_recordings(RECORDINGS_DIR))
    if cmd in ("transcript", "retranscribe"):
        if len(sys.argv) != 3:
            print(__doc__)
            sys.exit(1)
        if cmd == "transcript":
            sys.exit(print_transcript(RECORDINGS_DIR, sys.argv[2]))
        token = os.environ.get(TOKEN_ENV_VAR, "")
        if not token:
            print(f"ERROR: {TOKEN_ENV_VAR} environment variable not set")
            sys.exit(1)
        socket_url = load_config()["pai_cloud"]["socket_url"]
        sys.exit(retranscribe(RECORDINGS_DIR, sys.argv[2], socket_url, token))

    if cmd not in ("start", "stop", "status", "toggle"):
        print(__doc__)
        sys.exit(1)

    try:
        response = send_command(cmd)
    except (FileNotFoundError, ConnectionRefusedError):
        print("ERROR: Daemon not running. Run 'pai-stt setup' first.")
        sys.exit(1)
    print(response)
    sys.exit(0 if response.startswith("OK") else 1)


if __name__ == "__main__":
    main()
