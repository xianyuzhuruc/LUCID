from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


NODE = shutil.which("node")
INDEX_HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"


@unittest.skipUnless(NODE, "requires Node.js")
class NodeOrderingTests(unittest.TestCase):
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

    def test_local_is_first_and_remote_ids_use_natural_order(self) -> None:
        self.run_javascript(
            """
const dashboard = superCliTerminal();
const input = [
  { id: '200_docker', name: 'Docker' },
  { id: '147', name: 'Node 147' },
  { id: 'local', name: 'Local' },
  { id: '200', name: 'Node 200' },
  { id: '1', name: 'Node 1' },
  { id: '144', name: 'Node 144' },
];

const ordered = dashboard.orderNodes(input);

assert.notEqual(ordered, input);
assert.deepEqual(ordered.map(node => node.id), [
  'local', '1', '144', '147', '200', '200_docker',
]);
"""
        )

    def test_snapshot_and_nodes_refresh_apply_the_same_order(self) -> None:
        self.run_javascript(
            """
const dashboard = superCliTerminal();
const expected = ['local', '1', '144', '147', '200', '200_docker'];
const rows = expected.map(id => ({ id, name: id, health: 'healthy' }));

dashboard.applySnapshot({
  windows: [],
  counts: {},
  nodes: [rows[4], rows[2], rows[0], rows[5], rows[1], rows[3]],
});
assert.deepEqual(dashboard.nodes.map(node => node.id), expected);

globalThis.fetch = async () => ({
  async json() {
    return {
      nodes: [rows[5], rows[3], rows[1], rows[4], rows[0], rows[2]],
      ssh_history: [],
      local_enabled: true,
    };
  },
});
await dashboard.refreshNodes();
assert.deepEqual(dashboard.nodes.map(node => node.id), expected);
"""
        )


if __name__ == "__main__":
    unittest.main()
