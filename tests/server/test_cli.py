"""The installed console script's `serve` command answers for the installed package."""

import subprocess
import sys
import unittest
from pathlib import Path

from knowledgestore.server import __version__


class ConsoleScriptTest(unittest.TestCase):
    def test_version_flag_prints_installed_version(self):
        """Catches: the console script is not installed, or --version prints
        something other than the installed package version."""
        script = Path(sys.executable).parent / "knowledgestore"
        result = subprocess.run(
            [str(script), "serve", "--version"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(__version__, result.stdout)
        self.assertNotEqual(__version__, "0.0.0+uninstalled")


if __name__ == "__main__":
    unittest.main()
