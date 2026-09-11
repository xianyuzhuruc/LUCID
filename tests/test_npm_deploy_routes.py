from __future__ import annotations

import unittest

import app as lucid_app
from core.hub.nodes import NodeConfig


class NpmDeployRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_configured_node = lucid_app._configured_node
        self.original_start_job = getattr(lucid_app, "_start_npm_deploy_job", None)

    def tearDown(self) -> None:
        lucid_app._configured_node = self.original_configured_node
        if self.original_start_job is None:
            lucid_app.__dict__.pop("_start_npm_deploy_job", None)
        else:
            lucid_app._start_npm_deploy_job = self.original_start_job

    def test_route_starts_npm_job_for_the_requested_node(self) -> None:
        node = NodeConfig(id="node-a", kind="ssh", host="node.example", user="alice")
        captured: list[NodeConfig] = []
        lucid_app._configured_node = lambda node_id: node
        lucid_app._start_npm_deploy_job = lambda selected: captured.append(selected) or {
            "ok": True,
            "job_id": "npm-job-a",
            "status": "queued",
        }

        result = lucid_app.api_node_deploy_npm("node-a")

        self.assertEqual(captured, [node])
        self.assertEqual(result["job_id"], "npm-job-a")


if __name__ == "__main__":
    unittest.main()
