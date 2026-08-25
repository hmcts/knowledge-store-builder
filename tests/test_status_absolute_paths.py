"""A store's own files should hold paths relative to the store root, and nothing said so.

`store_paths` states the rule - relative at rest, absolute in flight - and until this
check nothing in the library called it and no document mentioned it, so a store author
had no way to discover it existed (#176).

The defect class is the silent kind, and that is what these tests have to defend
against. Both instances behind the rule - a corpus inventory written absolute, and a
`resolve()` that rewrote symlink paths into their targets' - passed every check a
maintainer would naturally run: the entry counts reconciled, the JSON stayed
well-formed, and every path in it existed on disk. So the tests below are mostly about
the ways a check like this reads as clean while measuring nothing, or measures a
neighbouring quantity and gets switched off:

- an absolute path in a compressed artefact, where the store's largest ones live
- a path straddling a read-block boundary, which a streaming scan loses in silence
- `/etc/hosts` and `/api/v1/things` reported as findings, which is "every absolute
  path" rather than "every path this store wrote absolute"
- a report of "none found" over files it never opened

Every fixture here is a real git repository with real `git add`, because the thing being
scoped is what git considers tracked. The only stub is the git seam itself, and only in
the test about git failing.
"""

from __future__ import annotations

import contextlib
import gzip
import io as _io
import subprocess
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import config, status  # noqa: E402

# A path from a machine that is not this one. It names a file inside a store's corpus,
# which is what makes it a finding rather than an unrelated absolute path.
FOREIGN = "/build-agent/work/a-store/repositories/infra/main.tf"


class StoreFixture(SettingsIsolated):
    """A real git repository, with the library pointed at it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolved, because `config` resolves the root it is given and on macOS
        # /var is a symlink to /private/var.
        self.root = Path(tmp.name).resolve()
        subprocess.run(
            ["git", "init", "-q", str(self.root)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        config.configure(root=str(self.root))

    def write(self, name: str, text: str, *, track: bool = True) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if track:
            self.track(name)
        return path

    def write_gz(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(text)
        self.track(name)
        return path

    def track(self, name: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), "add", "--", name],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def scan(self) -> dict:
        return status.absolute_paths_at_rest()


class WhatCounts(StoreFixture):
    def test_a_path_this_store_wrote_absolute_is_counted_and_its_file_named(self):
        """The finding itself. A count with no file name cannot be acted on, so both
        halves are asserted: remove the per-file tally and this fails."""
        self.write(
            "graphify-out/.graphify_chunk_plan.json",
            '{"0001": ["%s/repositories/infra/main.tf"]}' % self.root,
        )
        scan = self.scan()
        self.assertEqual(scan["findings"], {"graphify-out/.graphify_chunk_plan.json": 1})
        self.assertEqual(scan["files"], 1, "the scan must say what it actually read")
        self.assertGreater(scan["characters"], 0, "a scan that read nothing found nothing")

    def test_an_absolute_path_from_another_machine_is_still_a_finding(self):
        """The relocation case, and the reason this cannot be a prefix match on the
        current root: a path written on the build machine no longer starts with this
        store's root, and it is exactly the one that has already gone wrong."""
        self.write("knowledge/inventory.json", '{"files": ["%s"]}' % FOREIGN)
        self.assertEqual(self.scan()["findings"], {"knowledge/inventory.json": 1})

    def test_a_relative_path_is_not_a_finding(self):
        """The compliant form. If this failed, every correctly-written store would be
        reported and the check would be turned off on first contact."""
        self.write("knowledge/inventory.json", '{"files": ["repositories/infra/main.tf"]}')
        self.assertEqual(self.scan()["findings"], {})

    def test_an_absolute_path_outside_the_store_is_not_a_finding(self):
        """The quantity, not a neighbour of it. `/etc/hosts` and an API route are
        absolute and are not this store's paths; counting them measures "every absolute
        path", which is a different claim and a much noisier one."""
        self.write(
            "docs/notes.md",
            "reads /etc/hosts and posts to /api/v1/things via /usr/local/bin/tool\n",
        )
        self.assertEqual(self.scan()["findings"], {})

    def test_a_url_is_not_a_finding(self):
        """`https://host/repositories/x/y` contains the marker `store_paths` falls back
        to, so a check reading the marker without the lookbehind reports every link in
        the store's prose."""
        self.write("docs/notes.md", "see https://example.invalid/repositories/infra/main.tf\n")
        self.assertEqual(self.scan()["findings"], {})

    def test_a_glob_pattern_is_not_a_finding(self):
        """Config and docs are full of `**/*.py`. Matching `*` as a path segment turns
        every glob in the store into a finding."""
        self.write("docs/notes.md", "the glob is **/*.py and src/**/*.ts\n")
        self.assertEqual(self.scan()["findings"], {})


class WhatIsRead(StoreFixture):
    def test_a_compressed_tracked_file_is_read(self):
        """Where a store's largest path-bearing artefacts actually live. Skipping `.gz`
        would report a clean store while the archive it ships is full of them - the
        exact shape of the defect that motivated the rule."""
        self.write_gz("knowledge/intent/inventory.json.gz", '{"files": ["%s"]}' % FOREIGN)
        self.assertEqual(self.scan()["findings"], {"knowledge/intent/inventory.json.gz": 1})

    def test_a_path_straddling_a_read_block_boundary_is_counted_once(self):
        """The scan streams, so a path can be cut in half by a block boundary. Losing it
        is silent; counting both halves overstates the total. Two paths are written - one
        plainly inside the first block, one across the boundary - and the expected total
        of 2 is derived by hand from that, so both failures are visible."""
        padding = " " * (status.READ_BLOCK - 10 - len(FOREIGN))
        self.write("knowledge/inventory.json", FOREIGN + padding + FOREIGN + "\n")
        self.assertEqual(self.scan()["findings"], {"knowledge/inventory.json": 2})

    def test_a_path_spanning_the_hold_back_point_is_not_lost(self):
        """The defect this scan shipped in its own first draft, found by reconciling a
        2.4 MB fixture: 11,997 of the 12,000 paths written into it.

        A match ending past the hold-back point is deferred to the next pass, and the
        offset that pass resumed from was the hold-back point itself rather than the
        deferred match's own start. A path beginning before it and ending after it
        therefore had its head chopped off, the lookbehind refused what was left, and
        neither pass counted it. 0.03% wrong, in the direction that reads as clean."""
        start = status.READ_BLOCK - status.CARRY - 10
        head = " " * (start - len(FOREIGN))
        tail = " " * (status.READ_BLOCK + 100 - start - len(FOREIGN))
        self.write("knowledge/inventory.json", FOREIGN + head + FOREIGN + tail + "\n")
        self.assertEqual(self.scan()["findings"], {"knowledge/inventory.json": 2})

    def test_an_untracked_file_is_not_read(self):
        """Untracked build intermediates are regenerated on the next run and their shape
        is nobody's business. Reporting them makes the check unactionable."""
        self.write("knowledge/inventory.json", "clean\n")
        self.write("graphify-out/scratch.json", '{"files": ["%s"]}' % FOREIGN, track=False)
        scan = self.scan()
        self.assertEqual(scan["findings"], {})
        self.assertEqual(scan["listed"], 1, "only the tracked file should have been listed")

    def test_an_unreadable_tracked_file_is_named_rather_than_passed_over(self):
        """A file the scan could not open is not a file with nothing in it. Counting it
        as read would let a store whose artefacts are all unreadable report clean."""
        self.write_gz("knowledge/ok.json.gz", '{"files": ["%s"]}' % FOREIGN)
        self.write("knowledge/broken.json.gz", "this is not gzip at all\n")
        scan = self.scan()
        self.assertEqual(scan["unreadable"], ["knowledge/broken.json.gz"])
        self.assertEqual(scan["files"], 1)
        self.assertEqual(scan["findings"], {"knowledge/ok.json.gz": 1})


class TheIntendedException(StoreFixture):
    def test_an_in_flight_artefact_is_separated_from_the_findings(self):
        """graphify's FILE_LIST must be absolute verbatim, so an absolute path in it is
        the rule being kept, not broken. Filing it as a finding teaches the operator to
        ignore the line, and after that the check reports nothing to anybody."""
        self.write(
            "graphify-out/.graphify_uncached.txt", f"{self.root}/repositories/infra/main.tf\n"
        )
        scan = self.scan()
        self.assertEqual(scan["findings"], {})
        self.assertEqual(scan["in_flight"], {"graphify-out/.graphify_uncached.txt": 1})

    def test_the_exemptions_follow_the_configured_paths(self):
        """Hard-coded strings would silently stop exempting anything for a store that
        moves either file, and the operator would see two permanent false findings."""
        self.assertEqual(
            sorted(status.in_flight_artefacts()),
            ["graphify-out/.graphify_detect.json", "graphify-out/.graphify_uncached.txt"],
        )
        for reason in status.in_flight_artefacts().values():
            self.assertTrue(reason, "an exemption with no stated reason cannot be judged")


class TheReport(StoreFixture):
    def _report(self) -> str:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_absolute_paths(True)
        return out.getvalue()

    def test_the_finding_names_the_count_the_file_and_the_rule(self):
        self.write("knowledge/inventory.json", '{"files": ["%s"]}' % FOREIGN)
        text = self._report()
        self.assertIn("Absolute paths at rest: 1 in 1 of 1 tracked file(s) read", text)
        self.assertIn("knowledge/inventory.json (1)", text)
        self.assertIn("store_paths", text, "a report without the remedy is a puzzle")

    def test_a_clean_store_says_how_much_it_read(self):
        """A bare "none found" over an unknown amount of input is not a result. The file
        count and the characters read are what make the silence mean something."""
        self.write("knowledge/inventory.json", '{"files": ["repositories/infra/main.tf"]}')
        text = self._report()
        self.assertIn("none in 1 tracked file(s) read", text)
        self.assertIn("characters", text)

    def test_a_store_whose_files_could_not_be_read_is_not_reported_as_clean(self):
        """The vacuity case. `0 read, none found` printed as a pass is a defect this
        module has already shipped once, in the corpus-citation check."""
        self.write("knowledge/broken.json.gz", "this is not gzip at all\n")
        text = self._report()
        self.assertIn("nothing checked", text)
        self.assertNotIn("none in", text)

    def test_the_exemption_is_printed_with_its_reason(self):
        self.write(
            "graphify-out/.graphify_uncached.txt", f"{self.root}/repositories/infra/main.tf\n"
        )
        text = self._report()
        self.assertIn("by contract", text)
        self.assertIn("verbatim", text)

    def test_more_files_than_the_report_names_are_counted_but_summarised(self):
        """Determinism, and the tail. Six files means five named plus "and 1 more"; the
        ordering is by count then name so two runs read identically."""
        for index in range(6):
            self.write(f"knowledge/inventory-{index}.json", '{"f": ["%s"]}' % FOREIGN)
        text = self._report()
        self.assertIn("6 in 6 of 6 tracked file(s) read", text)
        self.assertIn("and 1 more", text)
        self.assertIn("knowledge/inventory-0.json (1)", text)
        self.assertNotIn("knowledge/inventory-5.json", text)


class WhenGitCannotAnswer(SettingsIsolated):
    def test_a_failed_listing_is_cannot_check_rather_than_nothing_found(self):
        """The store may not be a git repository at all. Returning an empty finding set
        would print a clean result for a store nothing looked at."""

        def refuse(_arguments):
            raise subprocess.CalledProcessError(128, "git")

        self.assertEqual(status.absolute_paths_at_rest(run=refuse), {"checked": False})

    def test_the_report_says_so(self):
        self.addCleanup(setattr, status, "absolute_paths_at_rest", status.absolute_paths_at_rest)
        status.absolute_paths_at_rest = lambda: {"checked": False}
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_absolute_paths(True)
        self.assertIn("could not be checked", out.getvalue())
        self.assertIn("Not a clean result", out.getvalue())


class ReachableFromTheCommandLine(StoreFixture):
    """Reporting through the function while nothing drives the CLI is the most repeated
    escape in this repository - four entries in the mutation gate are that shape. So the
    flag is driven, not the reporter."""

    def _main(self, argv: list[str]) -> str:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            self.assertEqual(status.main(argv), 0, "status must never return non-zero")
        return out.getvalue()

    def test_the_flag_reaches_the_check(self):
        self.write("knowledge/inventory.json", '{"files": ["%s"]}' % FOREIGN)
        self.assertIn("Absolute paths at rest: 1 in 1 of", self._main(["--paths"]))

    def test_without_the_flag_the_scan_does_not_run(self):
        """It reads every tracked file including the compressed ones, so it is opt-in
        like `--drift` and `--central`. A default-on scan changes what every existing
        `status` run costs."""
        self.write("knowledge/inventory.json", '{"files": ["%s"]}' % FOREIGN)
        self.assertNotIn("Absolute paths at rest", self._main([]))


if __name__ == "__main__":
    unittest.main()
