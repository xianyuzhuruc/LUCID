from __future__ import annotations

import unittest

from core.hub import ssh_deploy


class _Channel:
    def __init__(self, return_code: int) -> None:
        self.return_code = return_code

    def recv_exit_status(self) -> int:
        return self.return_code


class _Stream:
    def __init__(self, data: bytes, return_code: int = 0) -> None:
        self.data = data
        self.channel = _Channel(return_code)

    def read(self) -> bytes:
        return self.data


class _Client:
    def __init__(self, stdout: bytes, stderr: bytes, return_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code

    def exec_command(self, _command: str, timeout: int = 180):
        return (
            None,
            _Stream(self.stdout, self.return_code),
            _Stream(self.stderr),
        )


class RemoteCommandOutputTests(unittest.TestCase):
    def test_remote_path_ignores_successful_command_stderr(self) -> None:
        client = _Client(
            stdout=b"/root/.lucid/agent\n",
            stderr=(
                b"Your user's .npmrc file has a prefix setting, which is "
                b"incompatible with nvm.\n"
            ),
        )

        self.assertEqual(
            ssh_deploy._remote_expand(client, "~/.lucid/agent"),
            "/root/.lucid/agent",
        )

    def test_failed_command_reports_both_stdout_and_stderr(self) -> None:
        client = _Client(
            stdout=b"partial output\n",
            stderr=b"nvm warning and command failure\n",
            return_code=1,
        )

        with self.assertRaises(RuntimeError) as raised:
            ssh_deploy._run(client, "false")

        message = str(raised.exception)
        self.assertIn("stdout=partial output", message)
        self.assertIn("stderr=nvm warning and command failure", message)


if __name__ == "__main__":
    unittest.main()
