"""Composition rule for `transcript` down frames.

The protocol renders every committed (`is_final`) segment in `seq` order,
followed by the latest non-final one — `PaiSttDaemon._on_transcript` is
where that rule lives. These exercise the state a reconnect or a dropped
frame could plausibly scramble: segments arriving out of `seq` order, and a
partial that must not survive the final segment replacing it.
"""

import unittest

from pai_stt.daemon import PaiSttDaemon


def _daemon() -> PaiSttDaemon:
    return PaiSttDaemon(config={"pai_cloud": {"socket_url": "wss://example/socket"}})


class TestTranscriptComposition(unittest.TestCase):
    def test_committed_segments_join_in_seq_order_even_when_delivered_out_of_order(
        self,
    ) -> None:
        daemon = _daemon()
        daemon._on_transcript("second", True, 1)
        daemon._on_transcript("first", True, 0)
        self.assertEqual(daemon.current_text, "first second")

    def test_latest_partial_follows_all_committed_segments(self) -> None:
        daemon = _daemon()
        daemon._on_transcript("hello", True, 0)
        daemon._on_transcript("wor", False, 1)
        self.assertEqual(daemon.current_text, "hello wor")

    def test_a_final_segment_clears_the_stale_partial_it_replaces(self) -> None:
        daemon = _daemon()
        daemon._on_transcript("wor", False, 1)
        daemon._on_transcript("world", True, 1)
        self.assertEqual(daemon.current_text, "world")

    def test_alternating_partials_and_finals_stay_in_order(self) -> None:
        daemon = _daemon()
        daemon._on_transcript("one", True, 0)
        daemon._on_transcript("two", True, 1)
        daemon._on_transcript("thr", False, 2)
        daemon._on_transcript("three", True, 2)
        daemon._on_transcript("fo", False, 3)
        self.assertEqual(daemon.current_text, "one two three fo")


if __name__ == "__main__":
    unittest.main()
