from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app as lucid_app
from core.hub.auth import COOKIE_NAME, SESSION_MAX_AGE_SECONDS, AuthManager


class HubAuthRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name) / "state"
        self.auth = AuthManager(self.state_dir)
        self.original_auth = lucid_app.hub_auth_manager
        lucid_app.hub_auth_manager = self.auth
        self.env = patch.dict(os.environ, {"LUCID_MODE": "hub", "LUCID_AGENT_TOKEN": ""})
        self.env.start()
        self.client = TestClient(lucid_app.app)

    def tearDown(self) -> None:
        self.client.close()
        self.env.stop()
        lucid_app.hub_auth_manager = self.original_auth
        self.temp_dir.cleanup()

    def test_first_visit_redirects_to_public_setup_page_and_protects_api(self) -> None:
        root = self.client.get("/", follow_redirects=False)
        api = self.client.get("/api/windows")
        events = self.client.get("/api/events")
        openapi = self.client.get("/openapi.json", follow_redirects=False)
        login = self.client.get("/login")
        status_response = self.client.get("/api/auth/status")
        agent_health = self.client.get("/agent/v1/health")

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.json()["detail"], "authentication required")
        self.assertEqual(events.status_code, 401)
        self.assertEqual(openapi.status_code, 303)
        self.assertEqual(openapi.headers["location"], "/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn('id="password-confirm"', login.text)
        self.assertEqual(status_response.json(), {"configured": False, "authenticated": False})
        self.assertEqual(agent_health.status_code, 200)

    def test_setup_validates_confirmation_and_sets_fourteen_day_cookie(self) -> None:
        mismatch = self.client.post(
            "/api/auth/setup",
            json={"password": "correct horse battery staple", "confirm": "different password"},
        )
        short = self.client.post(
            "/api/auth/setup",
            json={"password": "short", "confirm": "short"},
        )
        created = self.client.post(
            "/api/auth/setup",
            json={"password": "correct horse battery staple", "confirm": "correct horse battery staple"},
        )

        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(short.status_code, 400)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json(), {"ok": True})
        set_cookie = created.headers["set-cookie"]
        self.assertIn(f"Max-Age={SESSION_MAX_AGE_SECONDS}", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie)
        self.assertTrue(self.client.cookies.get(COOKIE_NAME))
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertTrue(self.client.get("/api/auth/status").json()["authenticated"])

    def test_login_survives_manager_restart_and_logout_clears_cookie(self) -> None:
        password = "correct horse battery staple"
        self.auth.setup_password(password)
        wrong = self.client.post("/api/auth/login", json={"password": "wrong password"})
        logged_in = self.client.post("/api/auth/login", json={"password": password})
        token = self.client.cookies.get(COOKIE_NAME)

        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(logged_in.status_code, 200)
        self.assertTrue(token)

        lucid_app.hub_auth_manager = AuthManager(self.state_dir)
        restarted_client = TestClient(lucid_app.app)
        try:
            restarted_client.cookies.set(COOKIE_NAME, token)
            self.assertEqual(restarted_client.get("/").status_code, 200)
        finally:
            restarted_client.close()

        logged_out = self.client.post("/api/auth/logout")
        self.assertEqual(logged_out.status_code, 200)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)

    def test_corrupt_auth_config_fails_closed_but_login_page_stays_available(self) -> None:
        self.state_dir.mkdir(mode=0o700)
        self.auth.config_path.write_text("{broken", encoding="utf-8")

        self.assertEqual(self.client.get("/").status_code, 503)
        self.assertEqual(self.client.get("/api/windows").status_code, 503)
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/status").status_code, 503)

    def test_unauthenticated_hub_websocket_is_closed_with_4401(self) -> None:
        self.auth.setup_password("correct horse battery staple")

        with self.assertRaises(WebSocketDisconnect) as raised:
            with self.client.websocket_connect(
                "/api/nodes/missing/windows/bash/1/terminal/ws"
            ) as websocket:
                websocket.receive_text()

        self.assertEqual(raised.exception.code, 4401)

    def test_authenticated_main_page_exposes_logout_action(self) -> None:
        password = "correct horse battery staple"
        self.auth.setup_password(password)
        self.client.post("/api/auth/login", json={"password": password})

        page = self.client.get("/")

        self.assertEqual(page.status_code, 200)
        self.assertIn('@click="signOut()"', page.text)
        self.assertIn("/api/auth/logout", page.text)


if __name__ == "__main__":
    unittest.main()
