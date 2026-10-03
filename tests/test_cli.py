"""The CLI waits longer than a stop can take and reports a slow daemon without a traceback."""

import asyncio
import io
import unittest
import unittest.mock
from contextlib import redirect_stdout
from typing import Any

from pai_stt import cli
from pai_stt import daemon as daemon_module

CONFIG = {"transcription_timeout": 30}


class TestCommandTimeout(unittest.TestCase):
    def test_stop_and_toggle_outlast_the_transcription_wait(self) -> None:
        for command in ("stop", "toggle", "TOGGLE"):
            self.assertGreater(
                daemon_module.command_timeout(command, lambda: CONFIG),
                CONFIG["transcription_timeout"] + daemon_module.STOP_TAIL_S + 3,
            )

    def test_other_commands_do_not_need_the_config(self) -> None:
        def unreachable() -> dict[str, Any]:
            raise AssertionError("config must not be loaded")

        self.assertEqual(
            daemon_module.command_timeout("status", unreachable),
            daemon_module.DEFAULT_COMMAND_TIMEOUT_S,
        )


class TestSlowDaemon(unittest.TestCase):
    def test_toggle_that_times_out_exits_with_a_message_not_a_traceback(self) -> None:
        out = io.StringIO()
        with (
            unittest.mock.patch.object(cli.sys, "argv", ["pai-stt", "toggle"]),
            unittest.mock.patch.object(cli, "load_config", lambda: CONFIG),
            unittest.mock.patch.object(cli, "send_command", side_effect=TimeoutError),
            redirect_stdout(out),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("did not answer", out.getvalue())


class TestReplyToGoneClient(unittest.IsolatedAsyncioTestCase):
    async def test_reply_to_a_client_that_hung_up_does_not_raise(self) -> None:
        class Reader:
            async def read(self, _n: int) -> bytes:
                return b"STATUS"

        class Writer:
            closed = False

            def write(self, _data: bytes) -> None:
                raise BrokenPipeError

            async def drain(self) -> None:
                await asyncio.sleep(0)

            def close(self) -> None:
                self.closed = True

        daemon = daemon_module.PaiSttDaemon({})
        writer = Writer()
        await daemon_module._serve_command(daemon, Reader(), writer)
        self.assertTrue(writer.closed)


if __name__ == "__main__":
    unittest.main()
