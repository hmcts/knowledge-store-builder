"""No repository name reaches this public repository without being decided on.

This library is public. A store built with it is not, and neither are most of the
repositories in an estate — so naming one here discloses that it exists, which is
not publicly discoverable information. Four names had reached `main` before this
existed: two private repositories in test fixtures, one private repository quoted
in a docstring, and one internal repository named in `CLAUDE.md`.

**What this can and cannot check.** Visibility is a network fact, and a unit test
must not depend on the network — so this cannot tell you whether a name is private.
What it can do is refuse a name nobody has decided about: every repository-shaped
string in the tracked, public-facing files must appear in `PERMITTED` below, whose
entries were each checked with `gh repo view hmcts/<name> --json visibility`.

That makes adding a name a deliberate act with a one-command cost, which is the
most a test can honestly offer here. It also means a stale entry is possible: a
repository that was public when it was added and has since been made private will
still pass. Re-check the list when the estate changes shape.

**Siblings differ, so never generalise from one.** Two `cpp-ui-*` repositories in
one estate had opposite answers, and so did two `cpp-*-hearing` ones. The private
sibling is not named here, for the reason this whole file exists — which is also
how this check first earned its keep: it failed on its own docstring, where an
earlier draft named that repository while explaining not to.

It passed locally before that and failed in CI, because the scan reads
`git ls-files` and a newly written, unstaged file is invisible to it. A check whose
first run cannot see itself is worth knowing about.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Repository-shaped: a known estate prefix followed by a hyphenated tail. Kept to
# real prefixes rather than "any hyphenated word", because the latter matches most
# of English and a check that fires constantly gets deleted.
REPO_SHAPED = re.compile(
    r"\b(?:cp|cpp|ccd|cnp|dtspo|sscs|prl|fpl|div|xui|pcq|rpa)-[a-z0-9][a-z0-9-]{2,40}"
)

# Only files that ship or are read by outsiders. Scratch and generated output are
# not the concern; what is published is.
SCANNED = ("src", "tests", "docs", "skills", "examples")
SCANNED_FILES = ("README.md", "CLAUDE.md")

# Each entry checked with `gh repo view hmcts/<name> --json visibility`.
PERMITTED = frozenset(
    {
        # Verified PUBLIC.
        "ccd-module-elastic-search",
        "cnp-module-key-vault",
        "cpp-context-hearing",
        "cpp-ui-hearing",
        # Verified not to exist: invented for fixtures, and deliberately so.
        "cp-example-portal",
        "cpp-ui-example",
        # Not a repository name at all - matched by shape only. `cpp-ui-` is the
        # bare prefix, which appears as a filter rule in a fixture.
        "cpp-ui-",
    }
)


def tracked_public_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z", *SCANNED, *SCANNED_FILES],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / name for name in listing.stdout.split("\0") if name]


def names_found() -> dict[str, list[str]]:
    """Repository-shaped string -> the files it appears in."""
    found: dict[str, list[str]] = {}
    for path in tracked_public_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in REPO_SHAPED.findall(text):
            found.setdefault(match, []).append(str(path.relative_to(ROOT)))
    return found


class NoUndecidedRepositoryNames(unittest.TestCase):
    def test_the_scan_reads_something(self):
        """A scan matching nothing would pass the check below vacuously.

        The pathspec is the fragile part: an earlier version of this search used
        `-- 'src/**'`, which git treats as a literal glob that matches no path, and
        it reported zero candidates over a tree containing six. The control is the
        only reason that was caught.
        """
        files = tracked_public_files()
        self.assertGreater(len(files), 50, "git ls-files returned almost nothing")
        self.assertTrue(any(p.name == "CLAUDE.md" for p in files), "CLAUDE.md was not scanned")
        self.assertTrue(names_found(), "the pattern matched nothing at all in the tree")

    def test_every_repository_name_has_been_decided_on(self):
        undecided = {n: f for n, f in names_found().items() if n not in PERMITTED}
        self.assertEqual(
            undecided,
            {},
            "Repository-shaped names not in PERMITTED. Check each with "
            "`gh repo view hmcts/<name> --json visibility`. If it is private or "
            "internal, replace it with an invented name that preserves whatever the "
            "test relies on; if it is public, add it to PERMITTED.",
        )

    def test_the_permitted_list_is_not_carrying_dead_entries(self):
        """A name nobody uses any more should leave, or the list stops meaning
        'decided' and starts meaning 'ever mentioned'."""
        unused = PERMITTED - set(names_found())
        self.assertEqual(unused, set(), f"PERMITTED entries no longer present: {unused}")


if __name__ == "__main__":
    unittest.main()
