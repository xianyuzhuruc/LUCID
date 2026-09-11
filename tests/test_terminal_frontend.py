from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


NODE = shutil.which("node")
INDEX_HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"


@unittest.skipUnless(NODE, "requires Node.js")
class TerminalFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        cls.application_script = html[html.rfind("<script>") + len("<script>") : html.rfind("</script>")]

    def run_javascript(self, assertions: str) -> None:
        source = (
            "import assert from 'node:assert/strict';\n"
            + self.application_script
            + "\n"
            + assertions
        )
        result = subprocess.run(
            [NODE or "node", "--input-type=module", "-"],
            input=source,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_hidden_terminal_container_retries_fit_when_it_becomes_visible(self) -> None:
        self.run_javascript(
            """
const animationFrames = [];
globalThis.window = {
  requestAnimationFrame(callback) {
    animationFrames.push(callback);
    return animationFrames.length;
  },
  cancelAnimationFrame() {},
};
globalThis.WebSocket = { OPEN: 1 };
const dashboard = superCliTerminal();
const sent = [];
let rectangleReads = 0;
dashboard.terminalWindow = { node_id: 'local' };
dashboard.terminal = { cols: 80, rows: 24 };
dashboard.terminalFitAddon = {
  fit() {
    dashboard.terminal.cols = 137;
    dashboard.terminal.rows = 41;
  },
};
dashboard.terminalSocket = {
  readyState: WebSocket.OPEN,
  send(payload) { sent.push(JSON.parse(payload)); },
};
dashboard.$refs = {
  terminalOutput: {
    getBoundingClientRect() {
      rectangleReads += 1;
      return rectangleReads === 1
        ? { width: 0, height: 0 }
        : { width: 1200, height: 700 };
    },
  },
};

dashboard.scheduleTerminalFitOnResize(0);
while (animationFrames.length) animationFrames.shift()();

assert.deepEqual(sent, [{ type: 'resize', cols: 137, rows: 41 }]);
"""
        )

    def test_hidden_new_bash_container_retries_fit_when_it_becomes_visible(self) -> None:
        self.run_javascript(
            """
const animationFrames = [];
globalThis.window = {
  requestAnimationFrame(callback) {
    animationFrames.push(callback);
    return animationFrames.length;
  },
  cancelAnimationFrame() {},
};
globalThis.WebSocket = { OPEN: 1 };
const dashboard = superCliTerminal();
const sent = [];
let rectangleReads = 0;
dashboard.editorTerminal = { cols: 80, rows: 24 };
dashboard.editorTerminalFitAddon = {
  fit() {
    dashboard.editorTerminal.cols = 131;
    dashboard.editorTerminal.rows = 38;
  },
};
dashboard.editorTerminalSocket = {
  readyState: WebSocket.OPEN,
  send(payload) { sent.push(JSON.parse(payload)); },
};
dashboard.$refs = {
  editorTerminalOutput: {
    getBoundingClientRect() {
      rectangleReads += 1;
      return rectangleReads === 1
        ? { width: 0, height: 0 }
        : { width: 1100, height: 650 };
    },
  },
};

dashboard.scheduleEditorTerminalFitOnResize(0);
while (animationFrames.length) animationFrames.shift()();

assert.deepEqual(sent, [{ type: 'resize', cols: 131, rows: 38 }]);
"""
        )

    def test_terminal_start_reports_missing_fit_addon_instead_of_using_80x24(self) -> None:
        self.run_javascript(
            """
let terminalConstructions = 0;
class FakeTerminal {
  constructor() { terminalConstructions += 1; }
}
globalThis.Terminal = FakeTerminal;
globalThis.window = { Terminal: FakeTerminal };
const dashboard = superCliTerminal();
const output = { textContent: '', innerHTML: '' };
dashboard.terminalWindow = { node_id: 'local' };
dashboard.$refs = { terminalOutput: output };

try {
  await dashboard.startTerminalSession();
} catch (_) {
  // Existing code continues with a fixed-size terminal and may fail later in
  // this deliberately minimal browser harness. The assertion below captures
  // the required behavior at the dependency boundary.
}

assert.equal(terminalConstructions, 0);
assert.match(output.textContent, /FitAddon/);
"""
        )

    def test_new_bash_start_reports_missing_fit_addon_instead_of_using_80x24(self) -> None:
        self.run_javascript(
            """
let terminalConstructions = 0;
class FakeTerminal {
  constructor() { terminalConstructions += 1; }
}
globalThis.Terminal = FakeTerminal;
globalThis.window = { Terminal: FakeTerminal };
const dashboard = superCliTerminal();
const output = { textContent: '', innerHTML: '' };
dashboard.$refs = { editorTerminalOutput: output };

try {
  await dashboard.startEditorTerminalSession({ tmux_session: 'lucid-editor-test' });
} catch (_) {}

assert.equal(terminalConstructions, 0);
assert.match(output.textContent, /FitAddon/);
"""
        )

    def test_successful_resize_does_not_reconnect_the_websocket(self) -> None:
        self.run_javascript(
            """
globalThis.window = {
  requestAnimationFrame(callback) { callback(); return 1; },
  cancelAnimationFrame() {},
};
globalThis.WebSocket = { OPEN: 1 };
const dashboard = superCliTerminal();
let reconnectSchedules = 0;
dashboard.terminalWindow = { node_id: 'local' };
dashboard.terminal = { cols: 80, rows: 24 };
dashboard.terminalFitAddon = { fit() {} };
dashboard.terminalSocket = { readyState: WebSocket.OPEN, send() {} };
dashboard.$refs = {
  terminalOutput: {
    getBoundingClientRect() { return { width: 1000, height: 600 }; },
  },
};
dashboard.scheduleTerminalReconnectAfterResize = () => { reconnectSchedules += 1; };

dashboard.scheduleTerminalFitOnResize(0, { reconnectAfter: true });

assert.equal(reconnectSchedules, 0);
"""
        )

    def test_history_delete_button_precedes_resume_and_fork(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        start = html.index('<template x-for="s in historySessions"')
        end = html.index('<div class="px-5 py-2 border-t', start)
        history_row = html[start:end]

        delete_position = history_row.find("Delete</button>")
        self.assertGreaterEqual(delete_position, 0, "History row must expose a Delete button")
        resume_position = history_row.index("Resume</button>")
        fork_position = history_row.index("Fork</button>")

        self.assertLess(delete_position, resume_position)
        self.assertLess(delete_position, fork_position)
        self.assertIn('@click="deleteHistorySession(s)"', history_row)

    def test_history_delete_uses_node_route_and_refreshes_the_current_page(self) -> None:
        self.run_javascript(
            """
globalThis.window = { confirm() { return true; } };
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return {
    ok: true,
    async json() {
      return { ok: true, action: 'deleted', session_id: 'session/id' };
    },
  };
};
const dashboard = superCliTerminal();
assert.equal(typeof dashboard.deleteHistorySession, 'function');
const refreshedPages = [];
const messages = [];
dashboard.historyPage = 3;
dashboard.expandedHistoryId = 'remote 1:codex:session/id';
dashboard.timelineData = { events: [] };
dashboard.loadHistory = async page => { refreshedPages.push(page); };
dashboard.toast = message => { messages.push(message); };
const session = {
  node_id: 'remote 1',
  platform: 'codex',
  session_id: 'session/id',
  session_key: 'remote 1:codex:session/id',
  project_name: 'LUCID',
};

await dashboard.deleteHistorySession(session);

assert.equal(requests.length, 1);
assert.equal(requests[0].url, '/api/nodes/remote%201/sessions/codex/session%2Fid');
assert.equal(requests[0].options.method, 'DELETE');
assert.deepEqual(refreshedPages, [3]);
assert.equal(dashboard.expandedHistoryId, null);
assert.equal(dashboard.timelineData, null);
assert.deepEqual(messages, ['Deleted session']);
"""
        )

    def test_history_delete_cancellation_does_not_send_a_request(self) -> None:
        self.run_javascript(
            """
globalThis.window = { confirm() { return false; } };
let requests = 0;
globalThis.fetch = async () => { requests += 1; };
const dashboard = superCliTerminal();
assert.equal(typeof dashboard.deleteHistorySession, 'function');

await dashboard.deleteHistorySession({
  node_id: 'local',
  platform: 'claude',
  session_id: 'session-1',
});

assert.equal(requests, 0);
"""
        )

    def test_history_session_is_active_when_matching_managed_window_is_alive(self) -> None:
        self.run_javascript(
            """
const dashboard = superCliTerminal();
assert.equal(typeof dashboard.isHistorySessionActive, 'function');
dashboard.windows = [{
  node_id: 'worker-1',
  platform: 'codex',
  session_id: 'session-1',
  alive: true,
}];

assert.equal(dashboard.isHistorySessionActive({
  node_id: 'worker-1',
  platform: 'codex',
  session_id: 'session-1',
  is_alive: false,
}), true);
assert.equal(dashboard.isHistorySessionActive({
  node_id: 'worker-2',
  platform: 'codex',
  session_id: 'session-1',
  is_alive: false,
}), false);
"""
        )


if __name__ == "__main__":
    unittest.main()
