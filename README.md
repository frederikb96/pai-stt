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
- Two modes, both streaming microphone audio to a PAI Cloud voice socket: **live** is transcribed
  while you speak; **batch** is stored by the backend as a take and transcribed after stop (by a
  backend set up for it, already during pauses). A batch take keeps recording when the backend is
  unreachable; whatever it did not receive is uploaded from the local recording afterwards, and a
  take a daemon restart left unfinished is finished at the next start
- Automatic clipboard copy on completion, verified by reading the clipboard back
- GNOME panel indicator with full-text popup; the live line always shows the end of the transcript,
  the recording dot pulses with the microphone level, and a batch take shows its elapsed time and a
  scrolling level history
- Client-side silence gate: after a quiet stretch audio stops being sent (the backend keeps the
  session open); the last second is sent first when speech resumes. Auto mode adapts to the room.
- Stop waits for the backend's receipt that the take is finished, so the last words arrive
- A dropped connection is retried for as long as the recording runs and once more at stop;
  unacknowledged and queued audio is sent again. Audio the live text missed during a longer
  outage (a backend restart) is transcribed from the local recording and inserted between
  `STT-INTERRUPTED` markers; if the backend is still down at stop, the rest follows one marker
- Past recordings on disk (newest 50) with commands and a panel list to copy and re-transcribe them
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
pai-stt toggle        # Start a live recording, or stop the running one (bind to a shortcut)
pai-stt toggle-batch  # Start a batch recording, or stop the running one (a second shortcut)
pai-stt status        # Check if daemon is running
pai-stt start         # Start a live recording
pai-stt start-batch   # Start a batch recording
pai-stt stop          # Stop recording

pai-stt recordings               # List past recordings, newest first
pai-stt transcript <id|last>     # Print a past recording's transcript
pai-stt retranscribe <id|last>   # Transcribe a past recording again via the backend
```

Either toggle stops a running recording, whichever mode it was started in. A batch take's text
reaches the clipboard once the backend has finished the take; how long that takes after stop
depends on the backend and on how much of the take it transcribed while you spoke.

Recordings live in `~/.local/share/pai-stt/recordings/` as a 16 kHz WAV plus a JSON sidecar
each; an id is the full take id or any unique prefix.

**Output:**
- Clipboard: `stt-rec: <transcription>`
- File: `~/.tmp/pai-stt-YYYYMMDD-HHMMSS.txt`
- Live preview: `tail -f ~/.tmp/pai-stt-result.txt`

## GNOME Extension

Shows recording status in the top panel and provides the clipboard service described below. The
popup holds the full text, copy and start/stop items, and the recent recordings with their start
time, length and mode: clicking one copies its transcript, its button transcribes it again. The
icon stays in the panel between recordings (`show-when-idle`, in the extension's preferences) and
turns grey when the daemon is not running or the microphone delivers no signal.

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
`silence_gate` block (on, auto by default; `manual` uses a fixed threshold in dBFS) and `capture.gain_db` (fixed gain
in dB on the audio that is measured and sent; the local recording stays as captured). A missing
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

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
ruff check src/ tests/
mypy src/
python -m pytest tests/
node --check extension/extension.js && node --check extension/prefs.js
```

Pull requests run the same checks in CI.
