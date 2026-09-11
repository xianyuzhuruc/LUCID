from __future__ import annotations

import unittest
from unittest import mock

from core.hub import npm_deploy
from core.hub.nodes import NodeConfig


class NpmDeployWorkflowTests(unittest.TestCase):
    def test_workflow_installs_nvm_then_loads_bashrc_and_installs_latest_npm(self) -> None:
        commands: list[tuple[str, int]] = []
        progress: list[tuple[str, str]] = []

        def run(command: str, timeout: int) -> str:
            commands.append((command, timeout))
            if "LUCID_NPM_RESULT" in command:
                return "LUCID_NPM_RESULT nvm=0.40.7 node=v24.14.0 npm=11.9.0\n"
            return ""

        result = npm_deploy.deploy_npm_with_runner(
            run,
            progress=lambda step, message: progress.append((step, message)),
        )

        self.assertEqual(len(commands), 3)
        install_nvm, install_node_npm, verify = (command for command, _timeout in commands)
        self.assertIn("raw.githubusercontent.com/nvm-sh/nvm/master/install.sh", install_nvm)
        self.assertIn('source "$HOME/.bashrc"', install_node_npm)
        self.assertLess(
            install_node_npm.index('source "$HOME/.bashrc"'),
            install_node_npm.index("nvm install node --latest-npm"),
        )
        self.assertIn("nvm alias default node", install_node_npm)
        self.assertIn("npm install --global npm@latest", install_node_npm)
        self.assertIn("LUCID_NPM_RESULT", verify)
        self.assertEqual(
            [step for step, _message in progress],
            ["npm_install_nvm", "npm_install_node", "npm_verify"],
        )
        self.assertEqual(
            result,
            {"ok": True, "nvm_version": "0.40.7", "node_version": "v24.14.0", "npm_version": "11.9.0"},
        )

    def test_workflow_rejects_missing_version_result(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "did not return installed versions"):
            npm_deploy.deploy_npm_with_runner(lambda _command, _timeout: "")

    def test_remote_commands_run_in_bash(self) -> None:
        node = NodeConfig(id="node-a", kind="ssh", host="node.example", user="alice")
        client = mock.MagicMock()
        commands: list[str] = []

        def run(_client: object, command: str, **_kwargs: object) -> str:
            commands.append(command)
            if "LUCID_NPM_RESULT" in command:
                return "LUCID_NPM_RESULT nvm=0.40.7 node=v24.14.0 npm=11.9.0\n"
            return ""

        with (
            mock.patch.object(npm_deploy.paramiko, "SSHClient", return_value=client),
            mock.patch.object(npm_deploy.ssh_deploy, "_ssh_connect_kwargs", return_value={}),
            mock.patch.object(npm_deploy.ssh_deploy, "_run", side_effect=run),
        ):
            npm_deploy.deploy_npm_for_node(node)

        self.assertTrue(commands)
        self.assertTrue(all(command.startswith("bash -lc ") for command in commands))
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
