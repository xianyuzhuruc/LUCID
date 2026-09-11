from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from core.hub.auth import (
    SESSION_MAX_AGE_SECONDS,
    AlreadyConfiguredError,
    AuthConfigError,
    AuthManager,
    InvalidCredentialsError,
    LoginRateLimitedError,
    PasswordPolicyError,
)


class MutableClock:
    def __init__(self, value: float = 1_800_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class AuthManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name) / "state"
        self.clock = MutableClock()
        self.auth = AuthManager(self.state_dir, clock=self.clock)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_setup_hashes_password_and_locks_down_state_files(self) -> None:
        password = "correct horse battery staple"

        self.auth.setup_password(password)

        raw = self.auth.config_path.read_text(encoding="utf-8")
        config = json.loads(raw)
        self.assertNotIn(password, raw)
        self.assertEqual(config["kdf"], "scrypt")
        self.assertTrue(config["password_hash"])
        self.assertTrue(config["session_secret"])
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.auth.config_path.stat().st_mode), 0o600)
        self.assertTrue(self.auth.verify_password(password))
        self.assertFalse(self.auth.verify_password("definitely wrong"))

    def test_short_password_is_rejected_without_creating_config(self) -> None:
        with self.assertRaises(PasswordPolicyError):
            self.auth.setup_password("short")

        self.assertFalse(self.auth.config_path.exists())

    def test_existing_or_corrupt_config_cannot_be_overwritten_by_setup(self) -> None:
        self.state_dir.mkdir(mode=0o700)
        self.auth.config_path.write_text("{broken", encoding="utf-8")
        before = self.auth.config_path.read_bytes()

        with self.assertRaises(AuthConfigError):
            self.auth.is_configured()
        with self.assertRaises(AlreadyConfiguredError):
            self.auth.setup_password("another valid password")

        self.assertEqual(self.auth.config_path.read_bytes(), before)

    def test_session_is_signed_and_expires_after_fourteen_days(self) -> None:
        self.auth.setup_password("correct horse battery staple")
        token = self.auth.create_session()

        self.assertTrue(self.auth.verify_session(token))
        payload, signature = token.split(".", 1)
        replacement = "A" if signature[0] != "A" else "B"
        self.assertFalse(self.auth.verify_session(payload + "." + replacement + signature[1:]))

        self.clock.value += SESSION_MAX_AGE_SECONDS - 1
        self.assertTrue(self.auth.verify_session(token))
        self.clock.value += 2
        self.assertFalse(self.auth.verify_session(token))

    def test_login_attempts_are_temporarily_rate_limited(self) -> None:
        password = "correct horse battery staple"
        self.auth.setup_password(password)

        for _ in range(5):
            with self.assertRaises(InvalidCredentialsError):
                self.auth.login("wrong password", "198.51.100.10")

        with self.assertRaises(LoginRateLimitedError):
            self.auth.login(password, "198.51.100.10")

        self.clock.value += 301
        token = self.auth.login(password, "198.51.100.10")
        self.assertTrue(self.auth.verify_session(token))


if __name__ == "__main__":
    unittest.main()
