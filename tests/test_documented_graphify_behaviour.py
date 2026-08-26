"""The graphify behaviour the guide documents, asserted rather than described.

`docs/building-a-knowledge-store.md` §5 states where a `.graphifyignore` must sit
to exclude symlinked source files, in a table with four cells. Every one of those
cells is a claim about a **third-party** package's collection semantics, which is
the kind of claim that rots without anyone touching this repository: graphify
ships independently, and a change to its ignore handling would leave the guide
confidently wrong with nothing failing.

So these tests exist to fail. When one does, the finding is not "fix the code" -
this repository has no code here - it is "graphify changed, go correct the
guide". Each test names the sentence it guards.

Skipped when graphify is absent, since it is an optional dependency: the default
install has no opinion on any of this.

Two traps met while measuring the table, both of which produced a confidently
wrong answer first time:

- The first fixture used a directory named `env`, which graphify prunes as a
  marker-gated noise directory. It produced a correct-looking exclusion that was
  the noise filter rather than the ignore file. Directory names here are
  deliberately neutral.
- Passing a relative path makes the ignore file silently inert, because the
  loader resolves anchors while the walk does not. That is a documented sharp
  edge and the last test pins it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import content_set  # noqa: E402

try:
    from graphify.detect import detect
    from graphify.extract import collect_files

    HAS_GRAPHIFY = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_GRAPHIFY = False

needs_graphify = unittest.skipUnless(
    HAS_GRAPHIFY, "needs graphify (a peer CLI, not a dependency of this library)"
)


@needs_graphify
class DocumentedIgnorePlacementTest(unittest.TestCase):
    """Guards the placement table in `docs/building-a-knowledge-store.md` §5."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name).resolve()
        # A store root that is a git repository, holding a cloned repository that
        # is also one. The enclosing VCS root is what bounds the ignore search, so
        # both matter - see `_load_graphifyignore`'s walk ceiling.
        self.repos = self.store / "repositories"
        self.repo = self.repos / "repo-a"
        (self.repo / "stacks" / "live").mkdir(parents=True)
        for root in (self.store, self.repo):
            subprocess.run(["git", "init", "-q", str(root)], check=True)
        (self.repo / "main.tf").write_text('resource "x" "y" {}\n', encoding="utf-8")
        (self.repo / "stacks" / "live" / "main.tf").symlink_to(self.repo / "main.tf")

    def _collected(self, target: Path) -> set[str]:
        """The .tf files graphify would extract, relative to the repository."""
        return {
            str(path.relative_to(self.repo))
            for path in collect_files(target)
            if path.suffix == ".tf"
        }

    def _ignore(self, at: Path, pattern: str) -> None:
        for stale in (self.store, self.repos, self.repo):
            (stale / ".graphifyignore").unlink(missing_ok=True)
        at.mkdir(parents=True, exist_ok=True)
        (at / ".graphifyignore").write_text(pattern + "\n", encoding="utf-8")

    # -- the premise: a symlink and its target are both collected --------------

    def test_a_symlink_and_its_target_are_both_collected(self):
        """Guards: "a symlink and its target become two sets of nodes with
        identical content under two paths"."""
        self.assertEqual(
            self._collected(self.repo),
            {"main.tf", "stacks/live/main.tf"},
            "if this fails graphify now de-duplicates, and the hazard section is obsolete",
        )

    # -- the four cells of the documented table --------------------------------

    def test_inside_the_repository_excludes_for_a_per_repository_scan(self):
        self._ignore(self.repo, "stacks/live/")
        self.assertEqual(self._collected(self.repo), {"main.tf"})

    def test_inside_the_repository_is_ignored_for_a_single_root_scan(self):
        """Guards: "a file *below* the scan root is inert". This is the cell that
        makes the two placements mutually exclusive, so it is the one worth
        having - a reader who gets it wrong sees no error, just duplicates."""
        self._ignore(self.repo, "stacks/live/")
        self.assertEqual(
            self._collected(self.repos),
            {"main.tf", "stacks/live/main.tf"},
            "a below-root ignore file must not appear to work, or the table is wrong",
        )

    def test_at_the_scan_root_excludes_for_a_single_root_scan(self):
        self._ignore(self.repos, "repo-a/stacks/live/")
        self.assertEqual(self._collected(self.repos), {"main.tf"})

    def test_above_the_repositorys_vcs_root_is_ignored_for_a_per_repository_scan(self):
        """Guards: "ignored - above the repository's VCS root". The store-root
        placement is the one that survives `sync`, which is exactly why it
        matters that it does nothing for a per-repository scan."""
        self._ignore(self.store, "repositories/repo-a/stacks/live/")
        self.assertEqual(
            self._collected(self.repo),
            {"main.tf", "stacks/live/main.tf"},
            "the sync-surviving placement must not silently appear to work per-repository",
        )

    def test_the_store_root_pattern_is_valid_when_the_store_root_is_scanned(self):
        """A control for the two negative cases above.

        Both assert that an ignore file does nothing, and a mistyped pattern
        would satisfy them just as well as the behaviour they mean to pin. This
        proves the same pattern really does exclude when the store root IS the
        scan root, so those negatives are about placement rather than about a
        pattern that never matched anything.
        """
        self._ignore(self.store, "repositories/repo-a/stacks/live/")
        self.assertEqual(self._collected(self.store), {"main.tf"})

    # -- the sharp edge ---------------------------------------------------------

    def test_a_relative_target_makes_the_ignore_file_silently_inert(self):
        """Guards: "`collect_files()` called directly with a relative path
        silently does not apply `.graphifyignore`".

        Scoped to the API deliberately. The CLI resolves the path before
        scanning, so the documented invocation - `graphify update .` from inside
        a repository - honours the ignore file, and an earlier revision of the
        guide wrongly stated the trap as a general rule about extraction.
        """
        self._ignore(self.repo, "stacks/live/")
        import os

        cwd = Path.cwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo.parent)
        collected = {
            str(path) for path in collect_files(Path("repo-a")) if str(path).endswith(".tf")
        }
        self.assertEqual(
            len(collected),
            2,
            "if graphify now honours the ignore file for a relative target, the "
            "documented sharp edge is fixed and the guide should say so",
        )


@needs_graphify
class ExtractionCacheRetainsContentTest(unittest.TestCase):
    """Extracted content outlives the graph it was extracted into.

    `docs/building-a-knowledge-store.md` tells operators to exclude
    secret-bearing files *before* extraction, on the grounds that filtering them
    out of the published graph afterwards does not reach the extraction cache.
    That is a claim about a third-party package's on-disk behaviour, so it is
    asserted here rather than described - if graphify stops caching, or caches
    without the content, the advice needs rewriting rather than keeping.
    """

    def test_the_cache_holds_the_extracted_content_after_a_build(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "src" / "secret_shaped.py").write_text(
            "def connect():\n    return 'value-from-the-file'\n", encoding="utf-8"
        )

        from graphify.extract import collect_files, extract

        extract(collect_files(root), cache_root=root)

        cached = list((root / "graphify-out" / "cache").rglob("*.json"))
        self.assertTrue(cached, "no extraction cache was written; the guide's premise is stale")
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in cached]
        self.assertTrue(
            any(isinstance(p, dict) and p.get("nodes") for p in payloads),
            "the cache exists but holds no extracted nodes - filtering the graph would "
            "then be sufficient, and the guide should say so",
        )


if __name__ == "__main__":
    unittest.main()


@needs_graphify
class DetectClassificationOfNamedFormatsTest(unittest.TestCase):
    """The premise behind `content_set`'s two named-format rules, measured.

    `content_set` refuses a Terraform state file that reaches the content set and
    excludes storage-emulator dumps that reach it. Both rules rest on a claim
    about what **graphify's** detect pass classifies, and the two claims do not
    have the same answer:

        __azurite_db_*.json    classified, so the exclusion does work today
        *.tfstate              not classified, so the refusal cannot fire today

    That difference is why the two are described differently. The refusal is
    defence-in-depth against a peer change, not a fix for a live exposure, and
    these tests are what make that statement checkable rather than remembered. A
    state file is one extension-map entry away from being classified - detect
    already classifies a plain `.json` as code - so when the first test fails the
    finding is "the refusal is now live, re-describe it", not "fix this
    repository".

    Without these, every test of the refusal drives a hand-built detect result,
    and a hand-built fixture asserting its own premise cannot notice that the
    premise is false.
    """

    def _content_set(self, names: tuple[str, ...]) -> list[str]:
        """The content set the real detect pass produces for a tree of `names`.

        A git repository with no `.gitignore`, because detect honours one by
        default: a fixture that let an ignore file swallow the tree would report
        "not classified" for every file in it, which is the flattering reading of
        a broken fixture rather than a measurement.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        for name in names:
            (root / name).write_text('{"version": 4}\n', encoding="utf-8")
        return content_set.content_paths(detect(root))

    def test_a_terraform_state_file_is_not_classified_as_content(self):
        """Break it catches: graphify classifying the state format.

        This test exists to fail, and while it passes `content_set.secret_bearing`
        returns nothing on any real estate - which is precisely the claim the
        refusal's own prose has to make and cannot check for itself.

        The two controls in the same tree are load-bearing. Without them an empty
        fixture, a failed `git init` or an ignore file swallowing the tree would
        report "not classified" for everything and pass, which is how a check
        that reads as compliance comes to have looked at nothing.
        """
        content = self._content_set(
            ("terraform.tfstate", "terraform.tfstate.backup", "plain.json", "main.tf")
        )
        self.assertEqual(sorted(Path(path).name for path in content), ["main.tf", "plain.json"])
        self.assertEqual(
            content_set.secret_bearing(content),
            [],
            "graphify now classifies a state file as content, so the refusal in "
            "`content-set` is live rather than defence-in-depth. Re-describe it, and "
            "check whether the estate needs telling.",
        )

    def test_an_emulator_dump_is_classified_as_content(self):
        """Break it catches: graphify stopping classifying the dump.

        The exclusion earns its place only while these files do reach the content
        set. If detect stops classifying them, the exclusion is dead code that
        still prints a heading, and nothing else in the suite would notice -
        every other test of it builds the detect result by hand.
        """
        content = self._content_set(("__azurite_db_blob__.json",))
        self.assertEqual(
            [Path(path).name for path in content_set.emulator_dumps(content)],
            ["__azurite_db_blob__.json"],
            "graphify no longer classifies an emulator dump as content, so nothing "
            "reaches the exclusion in `content-set` and it is now dead code.",
        )
