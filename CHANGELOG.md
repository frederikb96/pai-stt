# Changelog

All notable changes to this project are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/), versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Takes no longer open with a click: on PipeWire versions that wrap `pw-record` output in a
  container header unless `--raw` is passed, that header was forwarded as audio.

### Changed

- The bearer token comes from the `token_command` config key (required) instead of
  an environment variable; the service unit no longer passes any token through its environment.
- Capture and the local recording start before the voice socket is connected; the audio captured
  meanwhile is sent once the socket is ready, and a failed connection keeps the recording for
  `retranscribe`.
- `pai-stt stop` and `toggle` wait longer than `transcription_timeout` and report a slow daemon
  with a message instead of a traceback.

### Added

- `capture.gain_db` (required, `0` leaves the audio unchanged): fixed gain on the audio the
  silence gate measures and the backend receives; the local recording stays as captured.
- Silence gate (auto or manual threshold, one-second pre-roll), announced to the backend with
  `silence`; `caps.silence_gate` on `hello`.
- Device identification on `hello` (hostname and the default PipeWire source).
- Past recordings on disk with rotation, and the `recordings`, `transcript` and `retranscribe`
  commands.
- The `take_done` receipt; stop waits for it (`transcription_timeout`, default 30 s) and the
  daemon copies the finished text to the clipboard.
- GNOME indicator: the panel label ellipsizes from the start, so the live line shows the end of
  the transcript.

- Voice socket client reading the connection for its whole life, so the liveness ping is answered,
  speaking the PAI Cloud protocol's hello/gate/audio handshake, the
  `ping`/`pong` liveness pair, and downlink control messages including `transcript`.
- Daemon: microphone capture via PipeWire, DBus service for the GNOME extension, Unix socket
  command interface, transcript composition (committed segments in order, plus the latest partial).
- CLI: setup, teardown, install-extension, start/stop/status/toggle.
- GNOME Shell extension: panel indicator, popup with full transcript, clipboard delivery.
