from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import app as lucid_app
from core.conversations import codex, history


class HistoryPhysicalDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.projects_dir = self.root / ".claude" / "projects"
        self.claude_sessions_dir = self.root / ".claude" / "sessions"
        self.history_jsonl = self.root / ".claude" / "history.jsonl"
        self.codex_sessions_dir = self.root / ".codex" / "sessions"
        self.projects_dir.mkdir(parents=True)
        self.claude_sessions_dir.mkdir(parents=True)
        self.codex_sessions_dir.mkdir(parents=True)
        self.history_jsonl.parent.mkdir(parents=True, exist_ok=True)

        self.patches = ExitStack()
        self.patches.enter_context(patch.object(history, "PROJECTS_DIR", self.projects_dir))
        self.patches.enter_context(patch.object(history, "HISTORY_JSONL", self.history_jsonl))
        self.patches.enter_context(
            patch.object(history, "CLAUDE_SESSIONS_DIR", self.claude_sessions_dir, create=True)
        )
        self.patches.enter_context(patch.object(codex, "CODEX_SESSIONS_DIR", self.codex_sessions_dir))
        history._cache = []
        history._cache_ts = 0

    def tearDown(self) -> None:
        self.patches.close()
        self.temp_dir.cleanup()

    def delete_session(self, platform: str, session_id: str) -> dict:
        delete_session = getattr(history, "delete_session", None)
        self.assertIsNotNone(delete_session, "history.delete_session must be implemented")
        return delete_session(platform, session_id)

    def test_claude_delete_removes_transcript_history_entries_and_dead_metadata(self) -> None:
        target = "11111111-1111-1111-1111-111111111111"
        other = "22222222-2222-2222-2222-222222222222"
        project = self.projects_dir / "-home-project"
        project.mkdir()
        target_transcript = project / f"{target}.jsonl"
        other_transcript = project / f"{other}.jsonl"
        target_transcript.write_text('{"type":"user"}\n', encoding="utf-8")
        other_transcript.write_text('{"type":"user"}\n', encoding="utf-8")

        target_metadata = self.claude_sessions_dir / "target.json"
        other_metadata = self.claude_sessions_dir / "other.json"
        target_metadata.write_text(json.dumps({"sessionId": target, "pid": 99999999}), encoding="utf-8")
        other_metadata.write_text(json.dumps({"sessionId": other, "pid": 99999998}), encoding="utf-8")
        lines = [
            json.dumps({"sessionId": target, "display": "first"}),
            "{malformed history line",
            json.dumps({"sessionId": other, "display": "keep"}),
            json.dumps({"sessionId": target, "display": "second"}),
        ]
        self.history_jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
        history._cache = [object()]
        history._cache_ts = 12345

        result = self.delete_session("claude", target)

        self.assertEqual(
            result,
            {
                "ok": True,
                "action": "deleted",
                "platform": "claude",
                "session_id": target,
                "deleted_files": 2,
                "removed_history_entries": 2,
            },
        )
        self.assertFalse(target_transcript.exists())
        self.assertFalse(target_metadata.exists())
        self.assertTrue(other_transcript.exists())
        self.assertTrue(other_metadata.exists())
        remaining_history = self.history_jsonl.read_text(encoding="utf-8")
        self.assertIn("{malformed history line", remaining_history)
        self.assertIn(other, remaining_history)
        self.assertNotIn(target, remaining_history)
        self.assertEqual(history._cache, [])
        self.assertEqual(history._cache_ts, 0)

    def test_codex_delete_resolves_exact_session_id_from_session_metadata(self) -> None:
        target = "33333333-3333-3333-3333-333333333333"
        other = target + "-other"
        target_dir = self.codex_sessions_dir / "2026" / "08" / "23"
        target_dir.mkdir(parents=True)
        target_rollout = target_dir / f"rollout-prefix-{target}.jsonl"
        other_rollout = target_dir / f"rollout-prefix-{other}.jsonl"
        target_rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": target}}) + "\n",
            encoding="utf-8",
        )
        other_rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": other}}) + "\n",
            encoding="utf-8",
        )

        result = self.delete_session("codex", target)

        self.assertEqual(result["deleted_files"], 1)
        self.assertEqual(result["removed_history_entries"], 0)
        self.assertFalse(target_rollout.exists())
        self.assertTrue(other_rollout.exists())

    def test_delete_rejects_path_traversal_and_preserves_files(self) -> None:
        outside = self.root / "escape.jsonl"
        outside.write_text("keep", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.delete_session("claude", "../escape")

        self.assertTrue(outside.exists())

    def test_delete_reports_missing_session_without_mutating_cache(self) -> None:
        history._cache = [object()]
        history._cache_ts = 9876

        with self.assertRaises(FileNotFoundError):
            self.delete_session("codex", "44444444-4444-4444-4444-444444444444")

        self.assertEqual(len(history._cache), 1)
        self.assertEqual(history._cache_ts, 9876)


class HistoryDeleteRouteTests(unittest.TestCase):
    def test_local_delete_refuses_an_active_managed_session(self) -> None:
        delete_local = getattr(lucid_app, "_delete_local_history_session", None)
        self.assertIsNotNone(delete_local, "local delete helper must be implemented")
        active_window = {
            "platform": "codex",
            "session_id": "active-session",
            "alive": True,
        }

        with (
            patch.object(lucid_app.registry, "managed_windows", return_value=[active_window]),
            patch.object(lucid_app.history, "delete_session") as physical_delete,
            self.assertRaises(HTTPException) as raised,
        ):
            delete_local("codex", "active-session")

        self.assertEqual(raised.exception.status_code, 409)
        physical_delete.assert_not_called()

    def test_local_delete_refuses_an_active_unmanaged_claude_session(self) -> None:
        delete_local = getattr(lucid_app, "_delete_local_history_session", None)
        self.assertIsNotNone(delete_local, "local delete helper must be implemented")

        with (
            patch.object(lucid_app.registry, "managed_windows", return_value=[]),
            patch.object(lucid_app.history, "_find_alive_pids", return_value={"active-claude"}),
            patch.object(lucid_app.history, "delete_session") as physical_delete,
            self.assertRaises(HTTPException) as raised,
        ):
            delete_local("claude", "active-claude")

        self.assertEqual(raised.exception.status_code, 409)
        physical_delete.assert_not_called()

    def test_local_delete_refuses_codex_resume_target_when_bound_id_changed(self) -> None:
        active_window = {
            "platform": "codex",
            "session_id": "new-bound-session",
            "alive": True,
            "current_task": "bash -l -i -c 'exec codex resume original-session'",
        }

        with (
            patch.object(lucid_app.registry, "managed_windows", return_value=[active_window]),
            patch.object(lucid_app.history, "delete_session") as physical_delete,
            self.assertRaises(HTTPException) as raised,
        ):
            lucid_app._delete_local_history_session("codex", "original-session")

        self.assertEqual(raised.exception.status_code, 409)
        physical_delete.assert_not_called()

    def test_remote_delete_is_forwarded_with_http_delete(self) -> None:
        route = getattr(lucid_app, "api_node_session_delete", None)
        self.assertIsNotNone(route, "node session DELETE route must be implemented")
        remote_node = SimpleNamespace(kind="ssh", id="worker-1", display_name="Worker 1")

        with (
            patch.object(lucid_app, "_configured_node", return_value=remote_node),
            patch.object(lucid_app.nodes, "forward", return_value={"ok": True}) as forward,
        ):
            result = route("worker-1", "codex", "session-1")

        self.assertEqual(result, {"ok": True})
        forward.assert_called_once_with(
            "worker-1",
            "DELETE",
            "/agent/v1/sessions/codex/session-1",
        )


if __name__ == "__main__":
    unittest.main()
