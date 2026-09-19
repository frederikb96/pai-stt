# Changelog

All notable changes to this project are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/), versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Voice socket client speaking the PAI Cloud protocol's hello/gate/audio handshake and downlink
  control messages.
- Daemon: microphone capture via PipeWire, DBus service for the GNOME extension, Unix socket
  command interface.
- CLI: setup, teardown, install-extension, start/stop/status/toggle.
- GNOME Shell extension: panel indicator, popup with full transcript, clipboard delivery.
