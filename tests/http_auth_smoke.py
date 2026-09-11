"""Real-HTTP smoke flow for the LUCID Hub password gate."""
from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def main() -> None:
    base_url = os.environ.get("LUCID_SMOKE_BASE_URL", "http://127.0.0.1:21994")
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        _NoRedirect(),
    )

    def request(path: str, *, method: str = "GET", payload: dict | None = None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
        try:
            with opener.open(req, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    status, headers, _ = request("/")
    assert status == 303 and headers["Location"] == "/login"

    status, _, body = request("/api/windows")
    assert status == 401 and json.loads(body)["detail"] == "authentication required"

    status, _, body = request("/login")
    assert status == 200 and b'id="password-confirm"' in body

    password = "smoke-test-password"
    status, headers, body = request(
        "/api/auth/setup",
        method="POST",
        payload={"password": password, "confirm": password},
    )
    cookie_header = headers.get("Set-Cookie", "")
    assert status == 200 and json.loads(body) == {"ok": True}
    assert "Max-Age=1209600" in cookie_header and "HttpOnly" in cookie_header
    assert status == 200 and any(cookie.name == "lucid_session" for cookie in cookie_jar)

    status, _, body = request("/")
    assert status == 200 and b"superCliTerminal" in body

    status, _, body = request("/api/auth/logout", method="POST")
    assert status == 200 and json.loads(body) == {"ok": True}
    status, _, _ = request("/")
    assert status == 303

    status, _, body = request(
        "/api/auth/login",
        method="POST",
        payload={"password": password},
    )
    assert status == 200 and json.loads(body) == {"ok": True}
    status, _, _ = request("/")
    assert status == 200
    print("real HTTP auth smoke flow: ok")


if __name__ == "__main__":
    main()
