# PAI STT

[![CI](https://github.com/frederikb96/pai-stt/actions/workflows/ci.yaml/badge.svg)](https://github.com/frederikb96/pai-stt/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Real-time dictation for Linux, streamed to a PAI Cloud backend over a WebSocket. Runs as a
systemd user daemon with keyboard shortcut control and a GNOME panel indicator.

**This is not a standalone speech-to-text tool.** It holds no transcription API key of its own;
every request goes to one specific privately-hosted PAI Cloud deployment. It is public because
the parts underneath it — the daemon shape, the GNOME extension, the wire protocol client — are
worth reading and reusing, and because a public repository gets unmetered CI.

**Features:**
- Stream microphone audio to a PAI Cloud voice socket while recording
- Automatic clipboard copy on completion, verified by reading the clipboard back
- GNOME panel indicator with full-text popup; the live line always shows the end of the transcript
- Client-side silence gate: after a quiet stretch audio stops being sent (the backend keeps the
  session open); the last second is sent first when speech resumes. Auto mode adapts to the room.
- Stop waits for the backend's receipt that the take is finished, so the last words arrive
- Past recordings on disk (newest 10) with commands to list, print and re-transcribe them
- No local speech-to-text credentials — authentication is a single bearer token for the backend

## Installation

Requires Python 3.11+, PipeWire, and wl-clipboard (Wayland).

```bash
pipx install git+https://github.com/frederikb96/pai-stt.git
pai-stt setup
```

Edit `~/.config/pai-stt/config.yaml` and set `pai_cloud.socket_url` to your deployment's voice
socket and `token_command` to a command that prints the bearer token (for example a
password-store lookup). The command runs for every recording and re-transcription; the
token is never stored in the config file, the environment or the service unit.

## Usage

```bash
pai-stt toggle    # Start/stop recording (bind this to a keyboard shortcut)
pai-stt status    # Check if daemon is running
pai-stt start     # Start recording
pai-stt stop      # Stop recording

pai-stt recordings               # List past recordings, newest first
pai-stt transcript <id|last>     # Print a past recording's transcript
pai-stt retranscribe <id|last>   # Transcribe a past recording again via the backend
```

Recordings live in `~/.local/share/pai-stt/recordings/` as a 16 kHz WAV plus a JSON sidecar
each; an id is the full take id or any unique prefix.

**Output:**
- Clipboard: `stt-rec: <transcription>`
- File: `~/.tmp/pai-stt-YYYYMMDD-HHMMSS.txt`
- Live preview: `tail -f ~/.tmp/pai-stt-result.txt`

## GNOME Extension

Shows recording status in the top panel, a scrollable popup with the full text, a copy button and
the clipboard service described below.

```bash
pai-stt install-extension     # From the repo folder; log out/in on Wayland to load new code
gnome-extensions enable pai-stt@frederikb.github.com
```

## Clipboard

The daemon hands the text to the GNOME Shell extension over DBus, so the compositor itself owns
the clipboard: no helper process that can die, no X11 chunked transfer, and the text survives a
daemon restart. Without the extension it falls back to `wl-copy`. Every delivery is read back with
`wl-paste` and compared; the journal line `Clipboard: N chars via shell|wl-copy` reports which
path delivered it.

## Configuration

Edit `~/.config/pai-stt/config.yaml`; all options are in `config.example.yaml`, including the
`silence_gate` block (on, auto by default; `manual` uses a fixed threshold in dBFS). A missing
option stops the daemon with a message naming it.

## Uninstall

```bash
pai-stt teardown
pipx uninstall pai-stt
```

## Requirements

- **PipeWire:** `pw-record` for audio capture, `pw-play` for sound feedback
- **wl-clipboard:** `wl-paste` for clipboard verification, `wl-copy` as fallback writer
- **A bearer token** for the configured PAI Cloud deployment, printed by `token_command`

## Tests

```bash
python -m pytest tests/
```
