"""The committed stores the server tests read are in the repository, not only on disk.

KSB's `.gitignore` ignores `knowledge/`, `graphify-out/` and `knowledge_context.md` at
any depth, because a stage run from the repository root drops them there. A store is
made of exactly those, so without a negation every store under `tests/server/` is
left out of a commit: a working tree that has them passes the suite, and a clean
checkout fails 182 tests with FileNotFoundError. Nothing in the suite could tell the
two apart, so it is held here.
"""

import subprocess
import unittest
from pathlib import Path

from library_store.generate import MAIN, PARTIAL, RELEASES, frozen

SERVER_TESTS = Path(__file__).resolve().parent
REPOSITORY = SERVER_TESTS.parent.parent


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY), *args], capture_output=True, text=True, check=False
    )


def in_a_checkout() -> bool:
    return git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def store_roots() -> list[Path]:
    """Every store directory `generate.py` writes, and the frozen releases it keeps."""
    builds = [MAIN, PARTIAL, *(frozen(version) for version in RELEASES)]
    return [root for build in builds for root in (build.store, build.no_index)]


@unittest.skipUnless(in_a_checkout(), "not a git checkout: tracking cannot be asked")
class CommittedStoresAreTracked(unittest.TestCase):
    def test_every_store_the_generator_names_has_its_manifest_tracked(self):
        """Catches: a store left out of the commit by an ignore rule, which a working
        tree that still holds it cannot show. The manifest is the file every server
        test opens first."""
        roots = store_roots()
        self.assertEqual(len(roots), 8, roots)
        for root in roots:
            manifest = root / "knowledge" / "store-manifest.json"
            with self.subTest(store=root.relative_to(SERVER_TESTS).as_posix()):
                self.assertTrue(manifest.is_file(), f"{manifest} is not on disk")
                tracked = git("ls-files", "--error-unmatch", str(manifest))
                self.assertEqual(tracked.returncode, 0, tracked.stderr)

    def test_no_file_under_the_server_tests_is_ignored(self):
        """Catches: any file under `tests/server/` that an ignore rule would leave out
        of a commit - a store file, a fixture - and which this checkout only has
        because it was written here. Bytecode caches are ignored on purpose."""
        listed = git(
            "ls-files", "--others", "--ignored", "--exclude-standard", str(SERVER_TESTS)
        ).stdout.splitlines()
        ignored = [name for name in listed if "__pycache__" not in name]
        self.assertEqual(ignored, [])


if __name__ == "__main__":
    unittest.main()
