"""Past recordings on disk.

Every take is written as `<take_id>.wav` (16 kHz mono PCM16, header patched
on each append so a crash leaves a playable file) next to a `<take_id>.json`
sidecar holding the transcript and where it came from. The newest
`KEEP_RECORDINGS` are kept. The duration is never stored: the WAV is its one
source.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Optional

SAMPLE_RATE = 16000
KEEP_RECORDINGS = 10
WAV_HEADER_BYTES = 44


def wav_header(data_bytes: int) -> bytes:
    """A canonical 44-byte header for 16 kHz mono PCM16 with `data_bytes` of samples."""
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
        + b"data"
        + struct.pack("<I", data_bytes)
    )


@dataclass(frozen=True)
class Recording:
    id: str
    started_at: str
    duration_ms: int
    transcript: str
    transcript_source: str
    transcript_complete: bool


def _wav(directory: Path, rec_id: str) -> Path:
    return directory / f"{rec_id}.wav"


def _sidecar(directory: Path, rec_id: str) -> Path:
    return directory / f"{rec_id}.json"


def _write_sidecar(directory: Path, rec_id: str, meta: dict[str, object]) -> None:
    tmp = _sidecar(directory, rec_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta))
    tmp.replace(_sidecar(directory, rec_id))


class RecordingWriter:
    """Appends one take's captured audio to disk."""

    def __init__(self, directory: Path, take_id: str) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = directory
        self.take_id = take_id
        self._bytes = 0
        self._file: Optional[BinaryIO] = open(_wav(directory, take_id), "wb")
        self._file.write(wav_header(0))
        self._file.flush()
        self._meta: dict[str, object] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "transcript": "",
            "transcript_source": "live",
            "transcript_complete": False,
        }
        _write_sidecar(directory, take_id, self._meta)

    def append(self, pcm: bytes) -> None:
        """Add samples and patch the header sizes so the file is valid after every call."""
        if self._file is None:
            raise RuntimeError("recording already finished")
        self._file.write(pcm)
        self._bytes += len(pcm)
        end = self._file.tell()
        self._file.seek(0)
        self._file.write(wav_header(self._bytes))
        self._file.seek(end)
        self._file.flush()

    def finish(self, transcript: str, complete: bool) -> None:
        """Close the audio, record the live transcript and drop recordings past the newest few."""
        if self._file is not None:
            self._file.close()
            self._file = None
        self._meta.update(
            transcript=transcript, transcript_source="live", transcript_complete=complete
        )
        _write_sidecar(self.directory, self.take_id, self._meta)
        prune(self.directory)


def list_recordings(directory: Path) -> list[Recording]:
    """Every recording on disk, newest first."""
    found: list[Recording] = []
    for sidecar in directory.glob("*.json"):
        wav = _wav(directory, sidecar.stem)
        if not wav.exists():
            continue
        meta = json.loads(sidecar.read_text())
        samples = max(wav.stat().st_size - WAV_HEADER_BYTES, 0) // 2
        found.append(
            Recording(
                id=sidecar.stem,
                started_at=meta["started_at"],
                duration_ms=samples * 1000 // SAMPLE_RATE,
                transcript=meta["transcript"],
                transcript_source=meta["transcript_source"],
                transcript_complete=meta["transcript_complete"],
            )
        )
    return sorted(found, key=lambda r: r.started_at, reverse=True)


def resolve(directory: Path, ref: str) -> Recording:
    """`last`, a full id, or a unique id prefix."""
    recordings = list_recordings(directory)
    if not recordings:
        raise LookupError("no recordings")
    if ref == "last":
        return recordings[0]
    matches = [r for r in recordings if r.id.startswith(ref)]
    if len(matches) != 1:
        raise LookupError(f"{'no' if not matches else 'several'} recordings match {ref!r}")
    return matches[0]


def store_transcript(directory: Path, rec_id: str, transcript: str, source: str) -> None:
    """Replace a recording's transcript; a stored one is always complete."""
    meta = json.loads(_sidecar(directory, rec_id).read_text())
    meta.update(transcript=transcript, transcript_source=source, transcript_complete=True)
    _write_sidecar(directory, rec_id, meta)


def prune(directory: Path, keep: int = KEEP_RECORDINGS) -> None:
    """Delete everything but the newest `keep` recordings."""
    for rec in list_recordings(directory)[keep:]:
        _wav(directory, rec.id).unlink(missing_ok=True)
        _sidecar(directory, rec.id).unlink(missing_ok=True)


def read_pcm(directory: Path, rec_id: str) -> bytes:
    """The recording's samples, without the header."""
    return _wav(directory, rec_id).read_bytes()[WAV_HEADER_BYTES:]
