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
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from pai_stt.daemon import TOKEN_ENV_VAR, send_command
from pai_stt.paths import CONFIG_DIR, CONFIG_FILE

SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
SERVICE_FILE = SYSTEMD_DIR / "pai-stt.service"
EXTENSION_UUID = "pai-stt@frederikb.github.com"

DEFAULT_CONFIG = """# pai-stt Configuration

log_level: info

pai_cloud:
  # Full wss:// URL of the PAI Cloud voice socket.
  socket_url: wss://your-pai-cloud-host/api/voice/socket

# Max seconds to wait for a finished transcription after stopping recording
transcription_timeout: 120
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


def main() -> NoReturn:
    """Entry point."""
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "toggle").lower()

    if cmd == "setup":
        sys.exit(setup())
    if cmd == "teardown":
        sys.exit(teardown())
    if cmd == "install-extension":
        sys.exit(install_extension())

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
