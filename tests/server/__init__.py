"""The MCP server's tests, discovered with the rest of the suite.

Two things here exist for `python3 -m unittest discover -s tests`, which is what CI
runs and which does not look inside a directory that is not a package.

The test modules import their fixtures as top-level packages (`fixture_store`,
`library_store`), which they did when this directory was a repository's `tests/`.
Putting the directory on `sys.path` keeps them importable unchanged.

The server needs the `server` extra. Without it every module here would fail at
import, which reads as a defect in the server rather than as a missing install, so
`load_tests` reports one skip that names the extra instead.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MISSING = "the MCP SDK is not installed: pip install -e '.[server]' to run the server tests"


class _ServerExtraMissing(unittest.TestCase):
    @unittest.skip(MISSING)
    def test_the_server_tests_need_the_server_extra(self):
        """Catches: nothing. It exists so the skip is visible in the run."""


def load_tests(loader, standard_tests, pattern):
    if importlib.util.find_spec("mcp") is None:
        return loader.loadTestsFromTestCase(_ServerExtraMissing)
    suite = unittest.TestSuite()
    suite.addTests(
        loader.discover(str(HERE), pattern or "test*.py", top_level_dir=str(HERE.parent))
    )
    return suite
