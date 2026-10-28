"""`knowledgestore serve` is a command, not a pipeline stage, and it works without the extra.

The server's own behaviour is held by `tests/server/`, which needs the `server`
extra. What is held here must hold without it: the default install has to say what
is missing rather than die on an import, and `serve` must not leak into the table
every stage-shaped check reads as "what a build runs".
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

from knowledgestore import cli

ROOT = Path(__file__).resolve().parent.parent

WITHOUT_MCP = (
    "import sys; sys.modules['mcp'] = None; sys.modules['mcp_types'] = None;"
    "from knowledgestore import cli; raise SystemExit(cli.main(['serve', '--version']))"
)


class ServeIsACommandNotAStage(unittest.TestCase):
    def test_serve_is_not_in_the_stage_table(self):
        """Catches: `serve` registered as a stage, so `STAGES`-driven checks (every
        stage documented, run order, the git-floor list) treat a read-only server as a
        step of a build."""
        self.assertNotIn("serve", cli.STAGES)
        self.assertEqual(cli.SERVE, "serve")

    def test_the_usage_names_it_apart_from_the_stages(self):
        """Catches: a command that exists and is listed nowhere, so nobody finds it."""
        usage = cli.usage()
        stage_lines = [line for line in usage.splitlines() if line.startswith("  ")]
        self.assertNotIn("serve", [line.split()[0] for line in stage_lines if line.split()])
        self.assertIn("knowledgestore serve", usage)

    def test_without_the_server_extra_it_says_what_to_install(self):
        """Catches: `serve` dying on a bare ModuleNotFoundError traceback when the
        MCP SDK is absent, which reads as a defect in the library and not as a missing
        extra. The SDK is made unimportable in a child process, so the real wiring runs."""
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run(
            [sys.executable, "-B", "-c", WITHOUT_MCP],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("hmcts-knowledge-store-builder[server]", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
