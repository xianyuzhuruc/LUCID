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

    def test_node_card_exposes_deploy_npm_button(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("Deploy NPM", html)
        self.assertIn('@click.stop="deployNpm(n)"', html)

    def test_deploy_npm_tracks_each_node_job_and_result(self) -> None:
        self.run_javascript(
            """
globalThis.setTimeout = callback => { callback(); return 1; };
const requests = [];
globalThis.fetch = async (url, options = {}) => {
  requests.push({ url, method: options.method || 'GET' });
  if ((options.method || 'GET') === 'POST') {
    return {
      ok: true,
      async text() {
        return JSON.stringify({ ok: true, job_id: 'npm-job-a', status: 'queued', step: 'queued', message: 'NPM deployment queued' });
      },
    };
  }
  return {
    ok: true,
    async text() {
      return JSON.stringify({
        ok: true,
        job_id: 'npm-job-a',
        status: 'succeeded',
        step: 'complete',
        message: 'NPM deployment complete',
        result: { node_id: 'node-a', nvm_version: '0.40.7', node_version: 'v24.14.0', npm_version: '11.9.0' },
      });
    },
  };
};

const dashboard = superCliTerminal();
await dashboard.deployNpm({ id: 'node-a', name: 'Node A', kind: 'ssh' });

assert.deepEqual(requests, [
  { url: '/api/nodes/node-a/deploy-npm', method: 'POST' },
  { url: '/api/nodes/deploy/jobs/npm-job-a', method: 'GET' },
]);
assert.equal(dashboard.npmDeployBusy['node-a'], false);
assert.equal(dashboard.npmDeployResults['node-a'].status, 'succeeded');
assert.match(dashboard.npmDeployResults['node-a'].summary, /node v24\.14\.0 · npm 11\.9\.0/);
"""
        )

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

    def test_codex_launch_exposes_editable_model_suggestions_and_max_effort(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        start = html.index("<h3 class=\"text-xs font-semibold mb-2\">Launch managed process</h3>")
        end = html.index("<button @click=\"launchManaged()\"", start)
        launch_panel = html[start:end]

        self.assertIn('x-model="launchForm.model"', launch_panel)
        self.assertIn(
            '@input="launchForm.model = $event.target.value; updateCodexLaunchCommand()"',
            launch_panel,
        )
        self.assertIn('list="codex-model-suggestions"', launch_panel)
        self.assertIn('<datalist id="codex-model-suggestions">', launch_panel)
        self.assertIn('<option value="gpt-5.6-sol">', launch_panel)
        self.assertIn('<option value="deepseek-flash">', launch_panel)
        self.assertNotIn("deepseek-v4-flash", launch_panel)
        self.assertIn('x-model="launchForm.reasoning_effort"', launch_panel)
        self.assertIn(
            '@change="launchForm.reasoning_effort = $event.target.value; updateCodexLaunchCommand()"',
            launch_panel,
        )
        self.assertIn('<option value="max">max</option>', launch_panel)

    def test_codex_launch_omits_blank_model_overrides(self) -> None:
        self.run_javascript(
            """
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return {
    ok: true,
    async text() { return JSON.stringify({ ok: true, tmux_session: 'codex-defaults' }); },
  };
};
const dashboard = superCliTerminal();
assert.equal(dashboard.launchForm.model, '');
assert.equal(dashboard.launchForm.reasoning_effort, '');
dashboard.launchForm.node_id = 'local';
dashboard.openLaunchedTerminal = async () => {};
dashboard.toast = () => {};

await dashboard.launchManaged();

const payload = JSON.parse(requests[0].options.body);
assert.equal(payload.command, 'codex');
assert.equal(Object.hasOwn(payload, 'model'), false);
assert.equal(Object.hasOwn(payload, 'reasoning_effort'), false);
"""
        )

    def test_codex_launch_writes_model_and_effort_into_command(self) -> None:
        self.run_javascript(
            """
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return {
    ok: true,
    async text() { return JSON.stringify({ ok: true, tmux_session: 'codex-custom' }); },
  };
};
const dashboard = superCliTerminal();
dashboard.launchForm.node_id = 'worker-1';
dashboard.launchForm.model = '  deepseek-flash  ';
dashboard.launchForm.reasoning_effort = 'max';
assert.equal(typeof dashboard.updateCodexLaunchCommand, 'function');
dashboard.updateCodexLaunchCommand();
dashboard.openLaunchedTerminal = async () => {};
dashboard.toast = () => {};

assert.equal(
  dashboard.launchForm.command,
  `codex --model deepseek-flash --config 'model_reasoning_effort="max"'`,
);
await dashboard.launchManaged();

const payload = JSON.parse(requests[0].options.body);
assert.equal(
  payload.command,
  `codex --model deepseek-flash --config 'model_reasoning_effort="max"'`,
);
assert.equal(Object.hasOwn(payload, 'model'), false);
assert.equal(Object.hasOwn(payload, 'reasoning_effort'), false);
"""
        )

    def test_codex_launch_replaces_generated_options_without_losing_custom_arguments(self) -> None:
        self.run_javascript(
            """
const dashboard = superCliTerminal();
assert.equal(typeof dashboard.updateCodexLaunchCommand, 'function');
dashboard.launchForm.command = 'codex resume session-1';
dashboard.launchForm.model = 'deepseek-flash';
dashboard.launchForm.reasoning_effort = 'max';

dashboard.updateCodexLaunchCommand();
assert.equal(
  dashboard.launchForm.command,
  `codex --model deepseek-flash --config 'model_reasoning_effort="max"' resume session-1`,
);

dashboard.launchForm.model = 'gpt-5.6-sol';
dashboard.launchForm.reasoning_effort = 'high';
dashboard.updateCodexLaunchCommand();
assert.equal(
  dashboard.launchForm.command,
  `codex --model gpt-5.6-sol --config 'model_reasoning_effort="high"' resume session-1`,
);
assert.equal((dashboard.launchForm.command.match(/--model/g) || []).length, 1);
assert.equal((dashboard.launchForm.command.match(/model_reasoning_effort/g) || []).length, 1);

dashboard.launchForm.model = '';
dashboard.launchForm.reasoning_effort = '';
dashboard.updateCodexLaunchCommand();
assert.equal(dashboard.launchForm.command, 'codex resume session-1');
"""
        )

    def test_claude_launch_ignores_stale_codex_overrides(self) -> None:
        self.run_javascript(
            """
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return {
    ok: true,
    async text() { return JSON.stringify({ ok: true, tmux_session: 'claude-default' }); },
  };
};
const dashboard = superCliTerminal();
dashboard.launchForm.node_id = 'local';
dashboard.launchForm.platform = 'claude';
dashboard.launchForm.command = 'claude';
dashboard.launchForm.model = 'deepseek-flash';
dashboard.launchForm.reasoning_effort = 'max';
dashboard.openLaunchedTerminal = async () => {};
dashboard.toast = () => {};

await dashboard.launchManaged();

const payload = JSON.parse(requests[0].options.body);
assert.equal(Object.hasOwn(payload, 'model'), false);
assert.equal(Object.hasOwn(payload, 'reasoning_effort'), false);
"""
        )


if __name__ == "__main__":
    unittest.main()
