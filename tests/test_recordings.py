"""Past recordings on disk, their rotation and the batch re-transcription pieces."""

import json
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from pai_stt import batch, cli, recordings
from pai_stt.device import parse_mic_description


def _record(directory: Path, take_id: str, samples: int, transcript: str = "") -> None:
    writer = recordings.RecordingWriter(directory, take_id)
    writer.append(bytes(samples * 2))
    writer.finish(transcript, complete=True)


class TestRecordings(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_wav_is_valid_after_every_append(self) -> None:
        writer = recordings.RecordingWriter(self.dir, "a")
        writer.append(bytes(3200))
        data = (self.dir / "a.wav").read_bytes()
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(int.from_bytes(data[40:44], "little"), 3200)
        self.assertEqual(len(data), 44 + 3200)

    def test_only_the_newest_ten_survive(self) -> None:
        for i in range(12):
            _record(self.dir, f"take-{i:02d}", 1600)
        kept = [r.id for r in recordings.list_recordings(self.dir)]
        self.assertEqual(len(kept), 10)
        self.assertNotIn("take-00", kept)
        self.assertNotIn("take-01", kept)
        self.assertEqual(kept[0], "take-11")
        self.assertEqual(len(list(self.dir.glob("*.wav"))), 10)

    def test_resolve_last_and_prefix(self) -> None:
        _record(self.dir, "abc111", 1600)
        _record(self.dir, "abd222", 1600)
        self.assertEqual(recordings.resolve(self.dir, "last").id, "abd222")
        self.assertEqual(recordings.resolve(self.dir, "abc").id, "abc111")
        with self.assertRaises(LookupError):
            recordings.resolve(self.dir, "ab")

    def test_transcript_command_prints_stored_text_and_refuses_an_empty_one(self) -> None:
        _record(self.dir, "t1", 1600, "hello there")
        _record(self.dir, "t2", 1600, "")
        with unittest.mock.patch("builtins.print") as printed:
            self.assertEqual(cli.print_transcript(self.dir, "t1"), 0)
            printed.assert_called_with("hello there")
            self.assertEqual(cli.print_transcript(self.dir, "t2"), 1)

    def test_listing_shows_every_recording(self) -> None:
        _record(self.dir, "t1", 16000, "first words")
        with unittest.mock.patch("builtins.print") as printed:
            cli.list_recordings(self.dir)
        self.assertIn("first words", printed.call_args.args[0])
        self.assertIn("1.0s", printed.call_args.args[0])

    def test_mic_description_is_parsed_from_wpctl_output(self) -> None:
        out = '  * node.name = "alsa_input.x"\n  * node.description = "Headset Mic"\n'
        self.assertEqual(parse_mic_description(out), "Headset Mic")
        self.assertIsNone(parse_mic_description("nothing"))


class TestBatchPieces(unittest.TestCase):
    def test_pieces_overlap_by_one_second_and_cover_everything(self) -> None:
        with unittest.mock.patch.object(batch, "PIECE_S", 4):
            pcm = bytes(range(256)) * (10 * 16000 * 2 // 256)  # 10 s
            parts = batch.pieces(pcm)
        second = 16000 * 2
        self.assertEqual([len(p) for p in parts], [4 * second, 4 * second, 4 * second])
        self.assertEqual(parts[0], pcm[: 4 * second])
        self.assertEqual(parts[1], pcm[3 * second : 7 * second])
        self.assertEqual(parts[2], pcm[6 * second :])

    def test_short_recording_is_one_piece(self) -> None:
        self.assertEqual(len(batch.pieces(bytes(3200))), 1)


class TestRetranscribe(unittest.TestCase):
    def test_pieces_go_up_in_order_with_the_text_so_far_and_the_result_is_stored(self) -> None:
        seen: list[bytes] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen.append(body)
                assert self.headers["Authorization"] == "Bearer tok"
                payload = json.dumps({"text": f"piece{len(seen)}"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_a: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _record(directory, "r1", 16000 * 6)
            with unittest.mock.patch.object(batch, "PIECE_S", 4):
                url = f"ws://127.0.0.1:{server.server_port}/api/voice/socket"
                code = cli.retranscribe(directory, "last", url, "tok")
            self.assertEqual(code, 0)
            rec = recordings.list_recordings(directory)[0]
        self.assertEqual((rec.transcript, rec.transcript_source), ("piece1 piece2", "batch"))
        self.assertNotIn(b"previous_text", seen[0])
        self.assertIn(b"piece1", seen[1])


if __name__ == "__main__":
    unittest.main()
