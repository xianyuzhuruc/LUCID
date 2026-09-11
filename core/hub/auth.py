"""Password authentication primitives for the LUCID Hub."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


COOKIE_NAME = "lucid_session"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 4096
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300

_CONFIG_VERSION = 1
_TOKEN_VERSION = 1
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32
_SESSION_SECRET_BYTES = 32


class AuthError(Exception):
    """Base class for Hub authentication failures."""


class AuthConfigError(AuthError):
    """The authentication configuration cannot be safely loaded."""


class AlreadyConfiguredError(AuthError):
    """A password already exists and setup must not overwrite it."""


class PasswordPolicyError(AuthError):
    """A password does not meet the local password policy."""


class InvalidCredentialsError(AuthError):
    """The supplied password is invalid."""


class LoginRateLimitedError(AuthError):
    """Too many failed attempts were made in the active window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("too many login attempts")
        self.retry_after = max(1, int(retry_after))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: Any, *, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise AuthConfigError(f"invalid {field}")
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthConfigError(f"invalid {field}") from exc


def _password_bytes(password: Any, *, enforce_policy: bool) -> bytes:
    if not isinstance(password, str):
        if enforce_policy:
            raise PasswordPolicyError("password must be a string")
        return b""
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        if enforce_policy:
            raise PasswordPolicyError("password is too long")
        return b""
    if enforce_policy and len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must contain at least {MIN_PASSWORD_LENGTH} characters")
    return encoded


def _derive_password(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_HASH_BYTES,
    )


class _LoginAttemptLimiter:
    def __init__(
        self,
        *,
        clock: Callable[[], float],
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: int = LOGIN_WINDOW_SECONDS,
    ) -> None:
        self._clock = clock
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        cutoff = now - self._window_seconds
        return [timestamp for timestamp in self._failures.get(key, []) if timestamp > cutoff]

    def retry_after(self, key: str) -> int:
        now = self._clock()
        with self._lock:
            failures = self._recent(key, now)
            if failures:
                self._failures[key] = failures
            else:
                self._failures.pop(key, None)
            if len(failures) < self._max_failures:
                return 0
            return max(1, int(self._window_seconds - (now - failures[0]) + 0.999))

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            failures = self._recent(key, now)
            failures.append(now)
            self._failures[key] = failures

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class AuthManager:
    """Owns password storage, signed sessions, and login attempt limiting."""

    def __init__(
        self,
        state_dir: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.config_path = self.state_dir / "auth.json"
        self._clock = clock
        self._setup_lock = threading.Lock()
        self._attempts = _LoginAttemptLimiter(clock=clock)

    def is_configured(self) -> bool:
        if not os.path.lexists(self.config_path):
            return False
        self._load_config()
        return True

    def setup_password(self, password: str) -> None:
        password_data = _password_bytes(password, enforce_policy=True)
        with self._setup_lock:
            if os.path.lexists(self.config_path):
                raise AlreadyConfiguredError("password is already configured")

            salt = secrets.token_bytes(_SALT_BYTES)
            config = {
                "version": _CONFIG_VERSION,
                "kdf": "scrypt",
                "n": _SCRYPT_N,
                "r": _SCRYPT_R,
                "p": _SCRYPT_P,
                "salt": _b64encode(salt),
                "password_hash": _b64encode(_derive_password(password_data, salt)),
                "session_secret": _b64encode(secrets.token_bytes(_SESSION_SECRET_BYTES)),
                "auth_version": _b64encode(secrets.token_bytes(16)),
                "created_at": int(self._clock()),
            }
            self._write_config(config)

    def verify_password(self, password: str) -> bool:
        password_data = _password_bytes(password, enforce_policy=False)
        if not password_data:
            return False
        config = self._load_config()
        candidate = _derive_password(password_data, config["salt_bytes"])
        return hmac.compare_digest(candidate, config["password_hash_bytes"])

    def create_session(self) -> str:
        config = self._load_config()
        payload = {
            "v": _TOKEN_VERSION,
            "iat": int(self._clock()),
            "av": config["auth_version"],
            "nonce": _b64encode(secrets.token_bytes(12)),
        }
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded_payload = _b64encode(payload_bytes)
        signature = hmac.new(
            config["session_secret_bytes"],
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded_payload}.{_b64encode(signature)}"

    def verify_session(self, token: str | None) -> bool:
        if not isinstance(token, str) or not token or len(token) > 4096 or token.count(".") != 1:
            return False
        config = self._load_config()
        encoded_payload, encoded_signature = token.split(".", 1)
        try:
            signature = _b64decode(encoded_signature, field="session signature")
            expected = hmac.new(
                config["session_secret_bytes"],
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected):
                return False
            payload_bytes = _b64decode(encoded_payload, field="session payload")
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (AuthConfigError, UnicodeDecodeError, json.JSONDecodeError, UnicodeEncodeError):
            return False

        if not isinstance(payload, dict) or payload.get("v") != _TOKEN_VERSION:
            return False
        issued_at = payload.get("iat")
        if not isinstance(issued_at, int) or isinstance(issued_at, bool):
            return False
        auth_version = payload.get("av")
        if not isinstance(auth_version, str) or not hmac.compare_digest(auth_version, config["auth_version"]):
            return False
        now = self._clock()
        if issued_at > now + 60:
            return False
        return now - issued_at <= SESSION_MAX_AGE_SECONDS

    def login(self, password: str, client_key: str) -> str:
        key = client_key or "unknown"
        retry_after = self._attempts.retry_after(key)
        if retry_after:
            raise LoginRateLimitedError(retry_after)
        if not self.verify_password(password):
            self._attempts.record_failure(key)
            raise InvalidCredentialsError("invalid password")
        self._attempts.clear(key)
        return self.create_session()

    def _load_config(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AuthConfigError("authentication is not configured") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthConfigError("authentication configuration is unreadable") from exc

        if not isinstance(raw, dict):
            raise AuthConfigError("authentication configuration must be an object")
        if raw.get("version") != _CONFIG_VERSION or raw.get("kdf") != "scrypt":
            raise AuthConfigError("unsupported authentication configuration")
        if (raw.get("n"), raw.get("r"), raw.get("p")) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            raise AuthConfigError("unsupported password parameters")

        salt = _b64decode(raw.get("salt"), field="password salt")
        password_hash = _b64decode(raw.get("password_hash"), field="password hash")
        session_secret = _b64decode(raw.get("session_secret"), field="session secret")
        auth_version = raw.get("auth_version")
        if len(salt) != _SALT_BYTES or len(password_hash) != _HASH_BYTES:
            raise AuthConfigError("invalid password material")
        if len(session_secret) != _SESSION_SECRET_BYTES:
            raise AuthConfigError("invalid session secret")
        if not isinstance(auth_version, str) or len(_b64decode(auth_version, field="auth version")) != 16:
            raise AuthConfigError("invalid auth version")

        return {
            **raw,
            "salt_bytes": salt,
            "password_hash_bytes": password_hash,
            "session_secret_bytes": session_secret,
        }

    def _write_config(self, config: dict[str, Any]) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.state_dir.chmod(0o700)
            fd, temporary_name = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=self.state_dir)
        except OSError as exc:
            raise AuthConfigError("authentication state directory is not writable") from exc

        temporary_path = Path(temporary_name)
        descriptor_open = True
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                descriptor_open = False
                json.dump(config, handle, ensure_ascii=True, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.config_path)
            self.config_path.chmod(0o600)
        except OSError as exc:
            raise AuthConfigError("authentication configuration could not be saved") from exc
        finally:
            if descriptor_open:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
