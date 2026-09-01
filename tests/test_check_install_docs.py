"""The install-command gate: documented commands must run as written.

The break this catches, from the estate this library was built for: the README
gave `pip install -r requirements.lock` as the rebuild step, while a comment ten
lines away in another file explained that the lock names no index and that this
exact command fails with "No matching distribution found". A comment saying
"remember the flag" is not a gate.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import check_install_docs as gate  # noqa: E402

FEED = "https://pkgs.example.com/feed/pypi/simple/"
LOCK_WITHOUT_INDEX = "thing==1.0 \\\n    --hash=sha256:abc\n"
LOCK_WITH_INDEX = f"--extra-index-url {FEED}\n--only-binary :all:\n\nthing==1.0\n"
# Names its index, so the documented-command half has nothing to say, and resolves a
# version the requirements input does not pin - which is the only thing left to fail.
LOCK_WITH_INDEX_WRONG_VERSION = (
    f"--extra-index-url {FEED}\n--only-binary :all:\n\nthing==2.0 \\\n    --hash=sha256:abc\n"
)


class InstallDocsGateTest(SettingsIsolated):
    def store(self, tmp: str, lock: str, files: dict[str, str]) -> Path:
        root = Path(tmp)
        (root / "requirements.lock").write_text(lock, encoding="utf-8")
        (root / "requirements.txt").write_text(
            f"--extra-index-url {FEED}\n--only-binary :all:\nthing==1.0\n", encoding="utf-8"
        )
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        config.configure(root=str(root))
        return root

    def test_a_lock_that_does_not_deliver_the_pin_fails_the_stage(self):
        """The wiring, not the comparison: `main` must carry the disagreement out.

        A check that prints a disagreement and returns 0 is worse than one that does
        not look - the text scrolls past in a green run and nothing chains on it. The
        comparison is tested directly elsewhere; this drives the stage, with the
        documented-command half deliberately satisfied so a non-zero exit can only
        have come from the resolution half.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, LOCK_WITH_INDEX_WRONG_VERSION, {})
            self.assertEqual(gate.main(), 1)

    def test_the_no_index_branch_still_carries_the_resolution(self):
        """The stage branch no other test reaches.

        Every other stage test uses a lock that names its index, so they all exit
        through the branch above this one. This is the shape a store takes when the
        lock carries no index and the documented commands pass the flag themselves -
        and a disagreement has to survive that path too. Found by review: there were
        two `return resolution` sites and the tests reached one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                "thing==2.0 \\\n    --hash=sha256:abc\n",
                {
                    "README.md": "Rebuild:\n\n```bash\npip install "
                    f"--extra-index-url {FEED} -r requirements.lock\n```\n"
                },
            )
            self.assertEqual(gate.main(), 1)

    def test_agreeing_files_leave_the_stage_passing(self):
        """The control. A stage that failed on an agreeing pair would be switched
        off, and its mutation result would prove nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, LOCK_WITH_INDEX, {})
            self.assertEqual(gate.main(), 0)

    def test_a_documented_command_that_cannot_work_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {"README.md": "Rebuild:\n\n```bash\npip install -r requirements.lock\n```\n"},
            )
            self.assertEqual(gate.main(), 1)

    def test_the_same_command_with_the_index_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {
                    "README.md": "Rebuild:\n\n```bash\npip install "
                    f"--extra-index-url {FEED} -r requirements.lock\n```\n"
                },
            )
            self.assertEqual(gate.main(), 0)

    def test_a_flag_on_a_continuation_line_still_counts(self):
        # the real command spans three lines; a line-at-a-time check would
        # report it as broken
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {
                    "README.md": "```bash\npip install --require-hashes \\\n"
                    f"  --extra-index-url {FEED} \\\n  -r requirements.lock\n```\n"
                },
            )
            self.assertEqual(gate.main(), 0)

    def test_prose_and_comments_about_the_failure_are_not_instructions(self):
        # a store is expected to document the trap without tripping this gate
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {
                    "CLAUDE.md": "`pip install -r requirements.lock` on its own fails.\n\n"
                    "```bash\n# `pip install -r requirements.lock` fails: no index\n"
                    f"pip install --extra-index-url {FEED} -r requirements.lock\n```\n"
                },
            )
            self.assertEqual(gate.main(), 0)

    def test_a_lock_that_names_its_index_needs_no_flag_anywhere(self):
        """Symmetric, so it survives the fix in either direction: recompiling
        the lock with --emit-index-url makes every command valid, and the gate
        goes quiet without being edited."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITH_INDEX,
                {"README.md": "```bash\npip install -r requirements.lock\n```\n"},
            )
            self.assertEqual(gate.main(), 0)

    def test_a_lock_naming_its_index_with_the_primary_directive_is_accepted(self):
        """Break it catches: matching only `--extra-index-url`.

        `--index-url` is pip's primary index directive and a lock carrying it
        names an index as plainly as one carrying the additional form. Reported
        from a store whose lock line 28 reads `--index-url https://pypi.org/simple`
        and which failed this stage permanently as a result - the check could not
        be satisfied by any correct lock, which is how a check gets skipped.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITH_INDEX.replace("--extra-index-url", "--index-url"),
                {"README.md": "```bash\npip install -r requirements.lock\n```\n"},
            )
            self.assertEqual(gate.main(), 0)

    def test_the_equals_joined_spelling_of_the_directive_is_read(self):
        """`--index-url=URL` is the same instruction as `--index-url URL`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.lock").write_text(
                LOCK_WITH_INDEX.replace(f"--extra-index-url {FEED}", f"--index-url={FEED}"),
                encoding="utf-8",
            )
            self.assertTrue(gate.declares_an_index(root / "requirements.lock"))
            self.assertEqual(gate.index_url(root / "requirements.lock"), FEED)

    def test_a_lock_naming_both_directives_is_still_accepted(self):
        """The common real topology, and the regression guard for this fix.

        `uv pip compile --emit-index-url` writes both lines when a store has a
        primary index plus a private feed. Such a lock passed before this change
        because it happens to carry the additional form, which is why the bug
        looked absent on one store and permanent on another: it is conditional on
        index topology, not on the tool. A later tightening that made the match
        exclusive would break exactly these stores.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                f"--index-url https://pypi.org/simple\n{LOCK_WITH_INDEX}",
                {"README.md": "```bash\npip install -r requirements.lock\n```\n"},
            )
            self.assertEqual(gate.main(), 0)

    def test_the_hint_names_the_extra_feed_rather_than_the_default_one(self):
        """Break it catches: reading whichever index directive comes first.

        An input naming PyPI primary and a private feed additionally is the
        ordinary shape. The package a failing command cannot find is on the
        private feed - PyPI is already the default, so a hint naming it tells the
        reader to add something that changes nothing. Accepting either directive
        without ranking them produced exactly that.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                f"--index-url https://pypi.org/simple\n--extra-index-url {FEED}\nthing==1.0\n",
                encoding="utf-8",
            )
            self.assertEqual(gate.index_url(root / "requirements.txt"), FEED)

    def test_the_hint_falls_back_to_the_only_directive_present(self):
        """A store whose private feed IS the primary index still gets a usable hint."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                f"--index-url {FEED}\nthing==1.0\n", encoding="utf-8"
            )
            self.assertEqual(gate.index_url(root / "requirements.txt"), FEED)

    def test_a_lock_naming_no_index_is_still_reported(self):
        """The over-correction guard, and the reason this pair is not one test.

        Accepting both directives must not become accepting anything. Widening
        the match until every lock 'names an index' would turn the whole check
        green and read exactly like the fix working. This is the assertion that
        separates the two.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {"README.md": "```bash\npip install -r requirements.lock\n```\n"},
            )
            self.assertEqual(gate.main(), 1)

    def test_a_comment_mentioning_the_directive_does_not_name_an_index(self):
        """The case where widening the match looks exactly like the fix working.

        A lock explaining itself - `# see --index-url docs`, or forty lines of
        commentary recording a version skew - names no index. A substring or
        prefix match passes it, and the failure is invisible: the stage goes green
        on a lock that names nothing, which is #277's vacuous pass arriving by a
        different route. Reported by a store verifying the fix against its own
        lock, which is the kind of lock that carries commentary.
        """
        self.assertIsNone(gate.index_directive("# see --index-url docs"))
        self.assertIsNone(gate.index_directive("#--index-url https://example.invalid"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "requirements.lock"
            lock.write_text(
                f"# this feed used to be {FEED} - see --extra-index-url notes\n"
                "thing==1.0 \\\n    --hash=sha256:abc\n",
                encoding="utf-8",
            )
            self.assertFalse(gate.declares_an_index(lock))

    def test_a_longer_option_that_merely_starts_the_same_way_is_not_an_index(self):
        """Whole-token matching: `--index-url-suffix` is not `--index-url`."""
        self.assertIsNone(gate.index_directive("--index-url-suffix https://example.invalid"))
        self.assertEqual(
            gate.index_directive("--index-url https://example.invalid"),
            ("--index-url", "https://example.invalid"),
        )

    def test_a_store_without_a_lock_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("no pins here\n", encoding="utf-8")
            config.configure(root=str(root))
            self.assertEqual(gate.main(), 0)

    def test_workflows_are_checked_outside_fences(self):
        # CI yaml has no fences; its run: blocks are the commands
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {".github/workflows/ci.yml": "steps:\n  - run: pip install -r requirements.lock\n"},
            )
            self.assertEqual(gate.main(), 1)

    def test_generated_directories_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                LOCK_WITHOUT_INDEX,
                {
                    "repositories/other/README.md": "```bash\npip install -r requirements.lock\n```\n"
                },
            )
            self.assertEqual(gate.main(), 0, "another repository's docs are not this store's")


class TheLockMustResolveThePinTest(unittest.TestCase):
    """A lock that does not deliver what the requirements input pins.

    `pip install --require-hashes -r <lock>` reads the lock, so when the two files
    disagree a build installs the lock's version while the store's requirements
    input says another. Nothing reported that: this check read the input only for
    an index URL to put in a hint, and the environment check compares the
    installed version against the lock, so three checks agreed about a file none
    of them had opened for correctness.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _files(self, requirements: str, lock: str) -> tuple[Path, Path]:
        req = self.root / "requirements.txt"
        lck = self.root / "requirements.lock"
        req.write_text(requirements, encoding="utf-8")
        lck.write_text(lock, encoding="utf-8")
        return req, lck

    def test_a_lock_delivering_another_version_is_reported(self) -> None:
        """The defect: a pin moved to a version the index had not published.

        The lock could not be recompiled, so it kept the previous version, and the
        store then claimed a version no build used.
        """
        req, lock = self._files("alpha==1.2.3\n", "alpha==1.2.4 \\\n    --hash=sha256:aa\n")
        self.assertEqual(gate.unresolved_pins(req, lock), [("alpha", "1.2.3", "1.2.4")])

    def test_a_pin_the_lock_omits_entirely_is_distinguished(self) -> None:
        """Resolving nothing installs no such package; resolving another installs
        the wrong one. The two need different fixes, so `None` is reported rather
        than folded into a version mismatch."""
        req, lock = self._files("alpha==1.2.3\n", "beta==9.9.9 \\\n")
        self.assertEqual(gate.unresolved_pins(req, lock), [("alpha", "1.2.3", None)])

    def test_extras_and_markers_do_not_read_as_a_disagreement(self) -> None:
        """`pkg[extra]==1.2.3 ; marker` asks for the same version as `pkg==1.2.3`.

        A lock never carries the pin's extras syntax, so comparing the raw strings
        would report every extras-bearing pin as unresolved and the check would be
        abandoned as noise.
        """
        # Asserted against a DISAGREEING lock, and on the reported mismatch rather
        # than on an empty list. `[]` is what "compared and agreed" and "never parsed
        # at all" both produce: delete the extras group from the pattern and an
        # extras-bearing pin silently stops being compared, which an emptiness
        # assertion cannot tell from agreement. Requiring the version to be reported
        # makes it survive the parse first.
        req, lock = self._files(
            "alpha[deploy,semantic]==1.2.3 ; python_version < '3.14'\n",
            "alpha==9.9.9 \\\n    --hash=sha256:aa\n",
        )
        self.assertEqual(gate.unresolved_pins(req, lock), [("alpha", "1.2.3", "9.9.9")])

    def test_names_are_compared_by_pep_503_normalisation(self) -> None:
        """A lock writes the normalised name, an input often writes the human one.

        Without normalisation `Al_pha.Beta` and `al-pha-beta` read as two packages
        and every such pin reports as unresolved.
        """
        req, lock = self._files("Al_pha.Beta==1.0\n", "al-pha-beta==1.0 \\\n")
        self.assertEqual(gate.unresolved_pins(req, lock), [])

    def test_a_requirement_that_is_not_pinned_is_not_compared(self) -> None:
        """`>=` states a range, and a lock resolving 2.0 satisfies `>=1.0`.

        Reporting that as a disagreement would be inventing one, and would make
        the check unusable for any store that does not pin everything exactly.
        """
        req, lock = self._files("alpha>=1.0\n", "alpha==2.0 \\\n")
        self.assertEqual(gate.unresolved_pins(req, lock), [])

    def test_a_commented_pin_is_not_read(self) -> None:
        """A store records superseded pins as comments; reading them would report a
        disagreement with a line that is deliberately inert.

        **This assertion documents the property; it does not gate it.** Review found
        it could not fail, and it still cannot: the behaviour is guaranteed twice over
        by construction. `_PINNED` opens with `^`, and both call sites use `re.match`,
        which anchors at position 0 regardless - so no single edit to either makes a
        commented line parse. Relaxing one of them to make this test meaningful would
        trade real defence for the appearance of coverage, which is the wrong way
        round; a redundantly guarded property is a good thing to have and a bad thing
        to count. It carries no mutation entry for the same reason.

        Two things were still worth changing. A `split("#", 1)[0]` strip stood in front
        of the anchoring and was provably inert - `^` blocks a leading `#`, and the
        version class already excludes `#`, so a trailing comment never reached the
        version either - and it read as the mechanism while doing nothing. And the
        commented pin now comes last: with it first, a parser that did read comments
        had its value overwritten by the live pin on the next line, so this could not
        catch even a doubly-broken parse. Ordered this way it would.
        """
        req, lock = self._files("alpha==1.2.3\n# alpha==9.9.9\n", "alpha==1.2.3 \\\n")
        self.assertEqual(gate.unresolved_pins(req, lock), [])

    def test_every_pin_is_checked_rather_than_one_named_package(self) -> None:
        """A store pinning several things must have all of them checked.

        Naming one package would leave the rest unguarded, and the library cannot
        know what a store pins.
        """
        req, lock = self._files("alpha==1.0\nbeta==2.0\n", "alpha==1.0 \\\nbeta==2.1 \\\n")
        self.assertEqual(gate.unresolved_pins(req, lock), [("beta", "2.0", "2.1")])

    def test_an_absent_requirements_input_is_not_a_disagreement(self) -> None:
        """A store may install without a requirements input. Reporting every locked
        package as unresolved there would fail a store that is doing nothing
        wrong."""
        lock = self.root / "requirements.lock"
        lock.write_text("alpha==1.0 \\\n", encoding="utf-8")
        self.assertEqual(gate.unresolved_pins(self.root / "absent.txt", lock), [])


class NothingToCompareTest(SettingsIsolated):
    """0 pins compared is not 0 pins wrong (#277).

    The break this catches: `_report_unresolved` printed "resolves every one of the
    0 pin(s)" and returned 0 when `pinned_versions` matched nothing - on the same
    path, in the same words and with the same exit code as a real pass, so no reader
    and no chained command could tell the two apart. The route in is not a store that
    deliberately pins nothing; it is the parse quietly ceasing to match a construct,
    which this library has shipped before in a `repositories.txt` reader that was
    green because every fixture used bare names.

    Each test drives `main()`, because the composed message and the exit code an
    operator meets are the artefacts that can be wrong.
    """

    PINNED = f"--extra-index-url {FEED}\n--only-binary :all:\nthing==1.0\n"
    UNPINNED = f"--extra-index-url {FEED}\n--only-binary :all:\nthing>=1.0\n"

    def store(self, tmp: str, requirements: str | None, lock: str, files: dict[str, str]) -> Path:
        root = Path(tmp)
        (root / "requirements.lock").write_text(lock, encoding="utf-8")
        if requirements is not None:
            (root / "requirements.txt").write_text(requirements, encoding="utf-8")
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        config.configure(root=str(root))
        return root

    def drive(self) -> tuple[int, str]:
        """The exit code and everything the operator is shown, both streams."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gate.main()
        return code, out.getvalue() + err.getvalue()

    def test_an_input_stating_no_pin_is_refused_rather_than_reported_as_resolved(self):
        """The defect. A requirements input carrying only a range states no `==` pin,
        which is also exactly what a parse that has stopped matching looks like from
        here - so the honest answer is not 0.

        The lock names its index, so the documented-command half has nothing to say
        and a non-zero exit can only have come from the resolution half.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, self.UNPINNED, LOCK_WITH_INDEX, {})
            code, shown = self.drive()
        self.assertEqual(code, 1)
        self.assertIn("states 0 `==` pins", shown, "the refusal must name the count")
        self.assertNotIn("resolves every one of the", shown)

    def test_an_absent_input_is_refused_rather_than_read_as_agreement(self):
        """The same emptiness from the other cause, and the likelier one: a store
        renames its input, or the reference layout's name changes, and the check reads
        a file that is not there. `unresolved_pins` returns [] for that by design, so
        without the guard the stage reports a clean comparison of a file it never
        opened."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, None, LOCK_WITH_INDEX, {})
            code, shown = self.drive()
        self.assertEqual(code, 1)
        self.assertIn("no requirements.txt", shown, "the refusal must name the missing input")
        self.assertNotIn("resolves every one of the", shown)

    def test_an_input_whose_pins_all_resolve_still_passes_and_still_says_so(self):
        """The sensitivity half, and the part that actually protects this: a fix that
        refused every empty-looking comparison, or refused outright, would pass the
        two tests above and be useless. This is the one that fails for it."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, self.PINNED, LOCK_WITH_INDEX, {})
            code, shown = self.drive()
        self.assertEqual(code, 0)
        self.assertIn("resolves every one of the 1 pin(s) requirements.txt states", shown)

    def test_no_documented_install_is_not_reported_as_every_install_passing(self):
        """The same shape one function along: "every documented install from it passes
        --extra-index-url" was printed over an empty list. The ways that list empties
        are all invisible from here - a documentation suffix the walk does not read, a
        `SKIP_DIRS` entry that grew to cover where the docs live, a flag spelling the
        pattern does not match - and every one of them reads as a store whose commands
        are all correct.

        Exit 0 is right, and is asserted: a store need not document installing from
        its lock at all. The claim is what was wrong, so this pins the wording.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                self.PINNED,
                LOCK_WITHOUT_INDEX,
                {"README.md": "Install the library, then build the store.\n"},
            )
            code, shown = self.drive()
        self.assertEqual(code, 0)
        self.assertIn("no documented command installs from it", shown)
        self.assertNotIn("every documented install", shown)

    def test_a_documented_install_that_passes_the_flag_is_counted(self):
        """The sensitivity half of the pair above: a message that always said it read
        nothing would pass that test and describe no store at all."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(
                tmp,
                self.PINNED,
                LOCK_WITHOUT_INDEX,
                {
                    "README.md": "```bash\npip install "
                    f"--extra-index-url {FEED} -r requirements.lock\n```\n"
                },
            )
            code, shown = self.drive()
        self.assertEqual(code, 0)
        self.assertIn("all 1 documented install(s) from it pass", shown)


if __name__ == "__main__":
    unittest.main()
