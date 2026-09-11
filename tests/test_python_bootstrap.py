from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class PythonBootstrapTests(unittest.TestCase):
    def test_bootstrap_installs_and_reuses_project_python_311(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_bin = tmp / "fake-bin"
            runtime = tmp / "runtime"
            calls = tmp / "calls.log"

            _write_executable(
                fake_bin / "uname",
                r"""
                #!/bin/sh
                if [ "$1" = "-s" ]; then printf '%s\n' Darwin; else printf '%s\n' arm64; fi
                """,
            )
            _write_executable(
                fake_bin / "curl",
                r"""
                #!/bin/sh
                printf 'curl %s\n' "$*" >> "$LUCID_TEST_CALLS"
                output=""
                while [ "$#" -gt 0 ]; do
                    if [ "$1" = "-o" ]; then output="$2"; shift 2; else shift; fi
                done
                : > "$output"
                """,
            )
            _write_executable(
                fake_bin / "tar",
                r"""
                #!/bin/sh
                output_dir=""
                while [ "$#" -gt 0 ]; do
                    if [ "$1" = "-C" ]; then output_dir="$2"; shift 2; else shift; fi
                done
                mkdir -p "$output_dir/bin"
                cat > "$output_dir/bin/micromamba" <<'SH'
                #!/bin/sh
                printf 'micromamba %s\n' "$*" >> "$LUCID_TEST_CALLS"
                prefix=""
                previous=""
                for argument in "$@"; do
                    if [ "$previous" = "-p" ]; then prefix="$argument"; fi
                    previous="$argument"
                done
                mkdir -p "$prefix/bin" "$prefix/conda-meta"
                cat > "$prefix/bin/python" <<'PY'
                #!/bin/sh
                exit 0
                PY
                chmod 755 "$prefix/bin/python"
                SH
                chmod 755 "$output_dir/bin/micromamba"
                """,
            )

            env = os.environ.copy()
            for name in ("LUCID_PYTHON", "LUCID_NO_VENV", "LUCID_PORT", "LUCID_HOST"):
                env.pop(name, None)
            env.update(
                {
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "LUCID_RUNTIME_DIR": str(runtime),
                    "LUCID_TEST_CALLS": str(calls),
                }
            )
            command = ["sh", str(REPO_ROOT / "scripts" / "bootstrap-python.sh")]

            first = subprocess.run(command, env=env, text=True, capture_output=True)
            second = subprocess.run(command, env=env, text=True, capture_output=True)

            expected_python = runtime / "python" / "bin" / "python"
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout.strip(), str(expected_python))
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout.strip(), str(expected_python))
            call_log = calls.read_text(encoding="utf-8")
            self.assertIn("micromamba create", call_log)
            self.assertIn("python=3.11", call_log)
            self.assertIn("/micromamba/osx-arm64/latest", call_log)
            self.assertEqual(call_log.count("micromamba "), 1)

    def test_run_sh_uses_bootstrapped_python_instead_of_system_python(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            project = tmp / "project"
            fake_bin = tmp / "fake-bin"
            runtime_python = tmp / "runtime-python"
            calls = tmp / "python-calls.log"
            system_calls = tmp / "system-python-calls.log"
            project.mkdir()
            (project / "scripts").mkdir()
            shutil.copy2(REPO_ROOT / "run.sh", project / "run.sh")
            (project / "scripts" / "check_port.py").write_text("", encoding="utf-8")

            _write_executable(
                project / "scripts" / "bootstrap-python.sh",
                fr"""
                #!/bin/sh
                printf '%s\n' '{runtime_python}'
                """,
            )
            _write_executable(
                runtime_python,
                r"""
                #!/bin/sh
                printf '%s\n' "$*" >> "$LUCID_TEST_CALLS"
                if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
                    mkdir -p "$3/bin"
                    cp "$0" "$3/bin/python"
                fi
                exit 0
                """,
            )
            _write_executable(
                fake_bin / "python3",
                r"""
                #!/bin/sh
                printf '%s\n' "$*" >> "$LUCID_TEST_SYSTEM_CALLS"
                exit 97
                """,
            )

            env = os.environ.copy()
            for name in ("LUCID_PYTHON", "LUCID_NO_VENV", "LUCID_PORT", "LUCID_HOST"):
                env.pop(name, None)
            env.update(
                {
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "LUCID_MODE": "hub",
                    "LUCID_RELOAD": "0",
                    "LUCID_RUNTIME_DIR": str(tmp / "runtime"),
                    "LUCID_TEST_CALLS": str(calls),
                    "LUCID_TEST_SYSTEM_CALLS": str(system_calls),
                }
            )
            result = subprocess.run(
                ["bash", "run.sh"],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("-m venv .venv", calls.read_text(encoding="utf-8"))
            self.assertFalse(system_calls.exists(), "run.sh invoked the system python3")


if __name__ == "__main__":
    unittest.main()
