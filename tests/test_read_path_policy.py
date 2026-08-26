"""Reads through `io` stay unconfined, and the policy permitting them is listed once.

SonarCloud `pythonsecurity:S8707` on `io.read_json` (code-scanning alert 53): a
path assembled from CLI arguments reaches `read_text` unvalidated, so whatever
built those arguments decides what the process reads. The taint flows the
analyser reports all have the same shape - `argparse` in `merge_layers`,
`chunk_status` and `extract_ast`, through `read_json_dict`, to the read in
`io.read_json` - which is why the answer belongs to the class rather than to one
line.

This repository already decided that answer for a read, in
`build_community_summaries.merge`: suppression with the grounds stated, not
validation. Reading a path the operator named is the purpose of the flag, and
this is an offline maintainer CLI against a local clone with no privilege
boundary to cross. The write-side guard deliberately does not transfer -
`checked_write_target` rejects an upward component because no caller in this
library needs to climb out of an output path it named, and a read whose entire
purpose is to open a path the caller chose has no equivalent property.

A suppression comment cannot be tested. The property it claims can, and so can
the change that would falsify it: someone reading the alert and confining reads.
Both confinements were measured on the write side - store-root confinement failed
48 tests, a configuration-derived allow-list failed 4 - and nothing on the read
path recorded that. These tests are that record. Confining `read_json` to
`config.ROOT` fails 106 tests in this suite, so without them the next attempt
learns nothing from 106 unrelated failures; with them it reads its answer off the
first one, and off the interface they name: a stage is documented to accept an
explicit path, and `record-clustering --graph <path>` legitimately points outside
the store.

Not duplicated here, because each already has a named observer: `read_json`'s
gzip dispatch and absent-file default are pinned in
`test_config_and_io.TheJsonReaderHandlesGzip`, behind the mutation entry "gzipped
graph unreadable again", and the write guard is pinned in `test_io_write_targets`.
The one write assertion below is the asymmetry itself, which neither file states.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io as pio  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src" / "knowledgestore"

# Where the grounds for suppressing S8707 are written down. Every other site
# cites this one instead of restating it, so the two cannot drift into two
# different policies.
MASTER = SRC / "build_community_summaries.py"

# The machine-readable half of that record: one line per module operating under
# the rule, so the register stays discoverable as sites are added.
POLICY_SITES = re.compile(r"S8707 policy site: (\S+\.py)\b")

# A suppression this gate must recognise wherever it appears. Matches both forms
# in the tree - `NOSONAR(S8707)` and `NOSONAR(S2083, S8707)`.
SUPPRESSION = re.compile(r"NOSONAR\([^)]*\bS8707\b")

# Below this the register has stopped parsing rather than the policy having
# shrunk. Five modules carry it today; removing one is a deliberate edit that
# lowers this line in the same change.
MINIMUM_SITES = 5


def suppressing_modules() -> list[str]:
    """Every module under `src/knowledgestore` that suppresses S8707, sorted."""
    return sorted(
        path.name for path in SRC.glob("*.py") if SUPPRESSION.search(path.read_text("utf-8"))
    )


def listed_sites(master_text: str) -> list[str]:
    """The modules the master's register names, sorted. Separate so sensitivity can call it."""
    return sorted(POLICY_SITES.findall(master_text))


def flattened(text: str) -> str:
    """Comment text with markers and line breaks removed, for phrase matching.

    Rewrapping a comment is a legitimate edit, and a phrase check that fails on
    it teaches people to delete the check. This asserts the words are present,
    not where the lines break.
    """
    return " ".join(text.replace("#", " ").split())


class ReadsAreNotConfinedTest(SettingsIsolated):
    """The documented interface the two rejected confinements would have broken."""

    def test_a_read_outside_the_configured_store_root_succeeds(self):
        """Breaks if reads are confined to the store root.

        The break it catches: a guard in `io` refusing a path outside
        `config.ROOT`. Stages are documented to take an explicit path -
        `record-clustering --graph <path>` - and an operator naming a graph that
        lives beside the store rather than inside it is supported use. On the
        write side the same guard failed 48 tests; here it would fail this one,
        with the reason on it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Resolved: `configure()` resolves the root, and on a platform where
            # the temporary directory is a symlink an unresolved comparison
            # reports "outside the root" for the wrong reason, making the
            # precondition pass while asserting nothing.
            store = Path(tmp).resolve() / "store"
            outside = Path(tmp).resolve() / "elsewhere"
            store.mkdir()
            outside.mkdir()
            config.configure(root=store)
            self.assertFalse(
                outside.is_relative_to(config.ROOT), "precondition: the file is outside the root"
            )

            plain = outside / "graph.json"
            plain.write_text(json.dumps({"nodes": [{"id": "a"}]}), encoding="utf-8")
            packed = outside / "graph.json.gz"
            pio.write_gzip_json(packed, {"nodes": [{"id": "b"}]})

            self.assertEqual(pio.read_json(plain), {"nodes": [{"id": "a"}]})
            self.assertEqual(pio.read_json(packed), {"nodes": [{"id": "b"}]})
            self.assertEqual(pio.read_gzip_json(packed), {"nodes": [{"id": "b"}]})
            self.assertEqual(pio.load_graph(plain), {"nodes": [{"id": "a"}]})

    def test_a_read_path_that_climbs_upward_is_accepted(self):
        """Breaks if `checked_write_target` is applied to the readers.

        The likeliest one-line answer to alert 53 is to call the guard that is
        already in the module, which would refuse every relative path an operator
        types from a subdirectory. All four readers are asserted because a
        half-wired refusal is exactly what reads as done.
        """
        with tempfile.TemporaryDirectory() as tmp:
            here = Path(tmp)
            (here / "sub").mkdir()
            (here / "graph.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
            pio.write_gzip_json(here / "graph.json.gz", {"nodes": [1]})

            climbing = here / "sub" / ".." / "graph.json"
            climbing_gz = here / "sub" / ".." / "graph.json.gz"

            self.assertEqual(pio.read_json(climbing), {"nodes": []})
            self.assertEqual(pio.read_json(climbing_gz), {"nodes": [1]})
            self.assertEqual(pio.read_gzip_json(climbing_gz), {"nodes": [1]})
            self.assertEqual(pio.load_graph(climbing), {"nodes": []})

    def test_the_same_upward_path_is_refused_for_writing(self):
        """Breaks if the write guard is weakened, or the asymmetry is levelled either way.

        One path, two verdicts: this is the whole policy in one assertion, and
        neither `test_io_write_targets` (writes only) nor the tests above (reads
        only) can state it. It fails if the guard stops rejecting `..`, and it
        fails if a future change makes reads and writes behave alike.
        """
        with tempfile.TemporaryDirectory() as tmp:
            here = Path(tmp)
            (here / "sub").mkdir()
            (here / "graph.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
            climbing = here / "sub" / ".." / "graph.json"

            self.assertEqual(pio.read_json(climbing), {"nodes": []})
            with self.assertRaises(ValueError) as caught:
                pio.write_json(climbing, {"nodes": ["overwritten"]})

            self.assertIn("traverses upward", str(caught.exception))
            self.assertEqual(
                json.loads((here / "graph.json").read_text(encoding="utf-8")),
                {"nodes": []},
                "the refused write must not have happened",
            )


class TheSuppressionPolicyIsListedOnceTest(unittest.TestCase):
    """The register of sites under the rule has to stay complete to be discoverable."""

    def setUp(self):
        self.master = MASTER.read_text(encoding="utf-8")
        self.listed = listed_sites(self.master)

    def test_the_register_still_parses(self):
        """Breaks if the register is reformatted such that this gate reads nothing.

        Without it every assertion below would pass over an empty register - green,
        and checking nothing, which is the failure this file exists to prevent.
        """
        self.assertGreaterEqual(
            len(self.listed),
            MINIMUM_SITES,
            f"parsed only {len(self.listed)} modules from {MASTER.name}; the register format "
            "changed and this gate is no longer reading it",
        )

    def test_every_suppressing_module_is_listed(self):
        """Breaks if a module starts suppressing S8707 without registering there.

        This is the drift the master exists to prevent: another site reasoning the
        rule out again, in words that can differ from the recorded grounds.
        """
        self.assertEqual(
            suppressing_modules(),
            self.listed,
            f"the modules suppressing S8707 and the ones {MASTER.name} lists have diverged; "
            "the register is where the grounds are recorded, so add the site there in the "
            "same change",
        )

    def test_every_listed_module_still_suppresses(self):
        """Breaks if a site is left on the register after its suppression is gone.

        A register naming a module that no longer suppresses anything reads as
        coverage for something that is not there.
        """
        for name in self.listed:
            with self.subTest(module=name):
                path = SRC / name
                self.assertTrue(path.is_file(), f"{MASTER.name} lists {name}, which does not exist")
                self.assertRegex(path.read_text("utf-8"), SUPPRESSION)

    def test_the_master_states_the_grounds_rather_than_only_listing(self):
        """Breaks if the register survives an edit that removes the reasoning.

        The register says where the policy applies; only the grounds say why, and
        a bare register is what leaves the next reader to invent them.
        """
        prose = flattened(self.master)
        for phrase in (
            "reading a caller-supplied path is this maintainer CLI's purpose",
            "no privilege boundary to cross",
            "does not transfer to a read",
        ):
            self.assertIn(phrase, prose, f"{MASTER.name} no longer states: {phrase!r}")

    def test_this_gate_notices_an_unlisted_site(self):
        """The sensitivity check, in the same run.

        A gate that can only pass or fail cannot report that it has gone vacuous.
        Dropping one real module from a copy of the master's text must change what
        `listed_sites` returns; if the parse is ever weakened to match everything,
        this fails instead of the suite quietly protecting nothing.
        """
        self.assertEqual(
            self.listed, suppressing_modules(), "precondition: the register is complete"
        )

        dropped = self.listed[0]
        forged = self.master.replace(f"S8707 policy site: {dropped}", "", 1)
        self.assertEqual(
            listed_sites(forged),
            self.listed[1:],
            "removing a site from the register was not detected, so this gate is vacuous",
        )


if __name__ == "__main__":
    unittest.main()
