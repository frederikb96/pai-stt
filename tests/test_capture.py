"""The capture command and the gain stage."""

import struct
import unittest
import unittest.mock

from pai_stt import daemon as daemon_module
from pai_stt.gain import apply_gain


class TestCaptureCommand(unittest.TestCase):
    def test_raw_flag_is_passed_when_pw_record_has_it(self) -> None:
        with unittest.mock.patch.object(daemon_module, "_pw_record_supports_raw", lambda: True):
            command = daemon_module.capture_command()
        self.assertEqual(command[-2:], ["--raw", "-"])

    def test_raw_flag_is_left_out_when_pw_record_does_not_know_it(self) -> None:
        with unittest.mock.patch.object(daemon_module, "_pw_record_supports_raw", lambda: False):
            command = daemon_module.capture_command()
        self.assertNotIn("--raw", command)
        self.assertEqual(command[-1], "-")

    def test_support_is_read_from_the_help_text(self) -> None:
        daemon_module._pw_record_supports_raw.cache_clear()
        self.addCleanup(daemon_module._pw_record_supports_raw.cache_clear)
        for help_text, expected in (("  -a, --raw   RAW mode\n", True), ("  --rate\n", False)):
            daemon_module._pw_record_supports_raw.cache_clear()
            done = unittest.mock.Mock(stdout=help_text, stderr="")
            with unittest.mock.patch.object(daemon_module.subprocess, "run", return_value=done):
                self.assertIs(daemon_module._pw_record_supports_raw(), expected)


class TestApplyGain(unittest.TestCase):
    def test_zero_gain_returns_the_same_bytes(self) -> None:
        pcm = struct.pack("<hh", 1, -2)
        self.assertIs(apply_gain(pcm, 0), pcm)

    def test_gain_scales_and_saturates_instead_of_wrapping(self) -> None:
        pcm = struct.pack("<hhhh", 100, -100, 20000, -20000)
        louder = struct.unpack("<hhhh", apply_gain(pcm, 6.0206))
        self.assertEqual(louder, (200, -200, 32767, -32768))
        quieter = struct.unpack("<hhhh", apply_gain(pcm, -6.0206))
        self.assertEqual(quieter, (50, -50, 10000, -10000))


if __name__ == "__main__":
    unittest.main()
