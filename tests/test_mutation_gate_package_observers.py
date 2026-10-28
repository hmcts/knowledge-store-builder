"""An observer in a package under `tests/` names its module, not its package.

The MCP server's tests live in `tests/server/`, so an observer there is
`server.test_x.Class.test`. `module_of` used to cut at the first dot and so named
`server`, which is a directory: `check_mapping` then reported every such observer as
a file that does not exist.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate as gate  # noqa: E402


class ObserversInPackages(unittest.TestCase):
    def test_a_top_level_module_is_everything_before_the_first_dot(self):
        """Catches: the new rule disturbing every shipped observer."""
        self.assertEqual(gate.module_of("test_io.Class.test_a"), "test_io")
        self.assertEqual(gate.module_of("test_io"), "test_io")

    def test_a_package_module_keeps_its_package(self):
        """Catches: `server` being taken as the module, so no observer under
        `tests/server/` can be run, mapped or found."""
        self.assertEqual(gate.module_of("server.test_x.Class.test_a"), "server.test_x")
        self.assertEqual(gate.module_of("server.test_x"), "server.test_x")

    def test_the_path_is_the_nested_file(self):
        """Catches: the file check looking for `tests/server.py`."""
        path = gate.observer_path("server.test_security_invariants.Class.test_a")
        self.assertEqual(path, gate.ROOT / "tests" / "server" / "test_security_invariants.py")
        self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
