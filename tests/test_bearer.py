"""The token resolver must fail loudly; an environment fallback would hide a broken command."""

import unittest
from unittest.mock import patch

from pai_stt.bearer import TokenError, resolve_token


class ResolveTokenTest(unittest.TestCase):
    def test_command_output_is_the_token(self) -> None:
        config = {"token_command": "sh -c 'printf from-command'"}
        self.assertEqual(resolve_token(config), "from-command")

    def test_failing_command_never_falls_back_to_environment(self) -> None:
        with patch.dict("os.environ", {"BEARER_TOKEN": "from-environment"}):
            with self.assertRaises(TokenError):
                resolve_token({"token_command": "false"})

    def test_command_printing_nothing_is_an_error(self) -> None:
        with self.assertRaises(TokenError):
            resolve_token({"token_command": "true"})

    def test_missing_executable_is_an_error(self) -> None:
        with self.assertRaises(TokenError):
            resolve_token({"token_command": "/nonexistent/pai-stt-fake-command"})

    def test_unconfigured_command_is_an_error_even_with_the_environment_set(self) -> None:
        with patch.dict("os.environ", {"BEARER_TOKEN": "from-environment"}):
            for config in ({}, {"token_command": ""}):
                with self.assertRaises(TokenError):
                    resolve_token(config)

    def test_error_message_never_contains_the_printed_token(self) -> None:
        config = {"token_command": "sh -c 'printf %s%s sec ret; exit 3'"}
        with self.assertRaises(TokenError) as ctx:
            resolve_token(config)
        self.assertNotIn("secret", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
