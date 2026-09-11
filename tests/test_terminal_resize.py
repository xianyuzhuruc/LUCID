from __future__ import annotations

import contextlib
import os
import pty
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import app as lucid_app


@unittest.skipUnless(os.name == "posix" and shutil.which("tmux"), "requires POSIX and tmux")
class TerminalResizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chmod(self.temp_dir.name, 0o700)
        self.env = dict(os.environ)
        self.env["TMUX_TMPDIR"] = self.temp_dir.name
        self.session_name = f"lucid-resize-test-{uuid.uuid4().hex[:10]}"
        self.tmux_bin = shutil.which("tmux") or "tmux"
        subprocess.run(
            [
                self.tmux_bin,
                "new-session",
                "-d",
                "-x",
                "80",
                "-y",
                "24",
                "-s",
                self.session_name,
                "sleep 30",
            ],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.master_fd = -1
        self.proc: subprocess.Popen[bytes] | None = None

    def tearDown(self) -> None:
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=2)
        if self.master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
        subprocess.run(
            [self.tmux_bin, "kill-session", "-t", self.session_name],
            env=self.env,
            check=False,
            capture_output=True,
        )
        self.temp_dir.cleanup()

    def _tmux_output(self, *args: str) -> str:
        result = subprocess.run(
            [self.tmux_bin, *args],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _wait_for(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for tmux to apply the terminal size")

    def test_resize_updates_tmux_window_started_in_a_new_session(self) -> None:
        resize_terminal_pty = getattr(lucid_app, "_resize_terminal_pty", None)
        self.assertIsNotNone(
            resize_terminal_pty,
            "terminal resize must notify the tmux attach process after updating the PTY",
        )

        self.master_fd, slave_fd = pty.openpty()
        lucid_app._set_pty_size(slave_fd, 24, 80)
        attach_env = dict(self.env)
        attach_env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
        self.proc = subprocess.Popen(
            lucid_app._terminal_attach_command(self.session_name),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            env=attach_env,
        )
        os.close(slave_fd)

        self._wait_for(
            lambda: bool(
                self._tmux_output(
                    "list-clients",
                    "-t",
                    self.session_name,
                    "-F",
                    "#{client_pid}",
                )
            )
        )

        resize_terminal_pty(self.master_fd, self.proc, 41, 137)

        self._wait_for(
            lambda: self._tmux_output(
                "display-message",
                "-p",
                "-t",
                self.session_name,
                "#{window_width}x#{window_height}",
            )
            == "137x41"
        )

    def test_resize_ignores_process_exit_during_signal_delivery(self) -> None:
        resize_terminal_pty = getattr(lucid_app, "_resize_terminal_pty", None)
        self.assertIsNotNone(resize_terminal_pty)

        class ExitingProcess:
            pid = 991337

            @staticmethod
            def poll():
                return None

        with (
            patch.object(lucid_app, "_set_pty_size") as set_size,
            patch.object(lucid_app.os, "killpg", side_effect=ProcessLookupError),
        ):
            resize_terminal_pty(17, ExitingProcess(), 36, 120)

        set_size.assert_called_once_with(17, 36, 120)


if __name__ == "__main__":
    unittest.main()
