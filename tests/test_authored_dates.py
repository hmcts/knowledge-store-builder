"""Each written-layer entry says when its text was authored.

Community summaries, topic briefs and deep dives are written once and kept across
rebuilds: `summaries remap` carries a summary onto its new community id, and a
merge keeps every entry whose text it did not change. Before #485 the only date a
store recorded was the manifest's `sources_as_of`, the newest source of the build
that *holds* the prose - so a reader serving a summary written three builds ago
dated it by today's build, and called old analysis current.

Each layer's metadata block now records, per entry, `authored_as_of` and
`authored_commit`: the build commit date and SHA, from provenance, of the build
that wrote that entry's text. Every test runs the real stage over real files and
derives the expected dates by hand. The breaks they name:

- a fresh entry left undated, or dated by something other than the build;
- a carried entry - by `remap`, exact or overlap, or by a merge that left its
  text alone - taking the carrying build's date: the defect itself;
- a re-authored entry keeping its old date, which dates new text as old;
- a date invented for an entry written before this change, or for one written
  where no build date can be read;
- the manifest losing the per-layer range, or comparing dates as text;
- two runs over one input giving different bytes (CLAUDE.md 1.3).
"""

from __future__ import annotations

import contextlib
import gzip
import io as stdio
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import build_deep_dives as dives  # noqa: E402
from knowledgestore import build_topic_briefs as briefs  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io  # noqa: E402
from knowledgestore import store_manifest  # noqa: E402

# Two builds, each recorded in provenance the way `sync` writes it. In the first,
# `alpha`'s date sorts last as text but is the earlier instant (09:00Z against
# 09:30Z), so a build date taken by comparing strings would name the wrong commit.
FIRST_BUILD = {
    "alpha": {"sha": "a" * 40, "branch": "main", "committed": "2026-03-01T10:00:00+01:00"},
    "beta": {"sha": "b" * 40, "branch": "main", "committed": "2026-03-01T09:30:00+00:00"},
}
FIRST_STAMP = {"authored_as_of": "2026-03-01T09:30:00+00:00", "authored_commit": "b" * 40}

SECOND_BUILD = {
    "alpha": FIRST_BUILD["alpha"],
    "beta": {"sha": "c" * 40, "branch": "main", "committed": "2026-05-02T08:00:00+00:00"},
}
SECOND_STAMP = {"authored_as_of": "2026-05-02T08:00:00+00:00", "authored_commit": "c" * 40}

# Long enough to clear both `MIN_BRIEF_LENGTH` and `MIN_DIVE_LENGTH`.
EVIDENCE = "Evidence sentence about one invented service. " * 30


def _quiet(call):
    with contextlib.redirect_stdout(stdio.StringIO()), contextlib.redirect_stderr(stdio.StringIO()):
        return call()


def _record_build(build: dict | None) -> None:
    """Provenance as `sync` leaves it, or none at all."""
    if build is None:
        config.PROVENANCE_PATH.unlink(missing_ok=True)
        return
    io.write_json(config.PROVENANCE_PATH, {"repositories": build}, indent=1)


def _authored(path: Path) -> dict | None:
    """The authored block of a layer's metadata, or None when it records none."""
    document = json.loads(path.read_text(encoding="utf-8"))
    return document[io.SUMMARIES_METADATA_KEY].get("authored")


class _Store(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        config.configure(root=str(self.root))
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        (self.root / "graphify-out").mkdir(parents=True)


# --- community summaries ------------------------------------------------------


def _prose(cid: str, version: str = "") -> str:
    return (
        f"Community {cid} groups the lookup and persistence paths that one invented "
        f"service reads at build time.{version}"
    )


# More communities than the remap's plausibility floor asks for.
_BACKGROUND = {str(i): [f"x{i}"] for i in range(1000, 1030)}


def _write_graph(members: dict[str, list[str]]) -> None:
    nodes = [
        {"id": node, "label": node, "community": int(cid)}
        for cid, ids in members.items()
        for node in ids
    ]
    config.GRAPH_PATH.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")


def _snapshot(members: dict[str, list[str]]) -> None:
    with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "wt", encoding="utf-8") as handle:
        json.dump(members, handle)


def _merge(root: Path, prose: dict[str, str], name: str) -> int:
    config.SUMMARIES_INPUT_PATH.write_text(
        json.dumps([{"id": cid} for cid in sorted(set(prose) | set(_BACKGROUND))]),
        encoding="utf-8",
    )
    batch = root / name
    batch.write_text(json.dumps(prose), encoding="utf-8")
    return _quiet(lambda: summaries.merge([str(batch)]))


class SummariesAreDatedByTheBuildThatWroteThem(_Store):
    def commit(self, prose: dict[str, str], name: str = "first.json") -> None:
        self.assertEqual(_merge(self.root, prose, name), 0, "the fixture did not merge")

    def test_a_fresh_summary_takes_the_build_date(self):
        """The gap: a merged summary recorded nothing about when it was written.
        Also breaks if the build date is chosen as text rather than as an instant -
        `alpha`'s string sorts last and is the earlier commit."""
        _record_build(FIRST_BUILD)
        self.commit({"7": _prose("7"), "8": _prose("8")})

        self.assertEqual(_authored(config.SUMMARIES_PATH), {"7": FIRST_STAMP, "8": FIRST_STAMP})

    def test_a_summary_merged_again_unchanged_keeps_its_date(self):
        """The break: a merge that re-dates every summary it touches. Merging the
        same text again is not writing it again, and neither is leaving a summary
        out of a batch."""
        _record_build(FIRST_BUILD)
        self.commit({"7": _prose("7"), "8": _prose("8")})
        _record_build(SECOND_BUILD)

        self.commit({"7": _prose("7"), "9": _prose("9")}, name="second.json")

        self.assertEqual(
            _authored(config.SUMMARIES_PATH),
            {"7": FIRST_STAMP, "8": FIRST_STAMP, "9": SECOND_STAMP},
        )

    def test_a_re_authored_summary_takes_the_new_build_date(self):
        """The break: a summary whose text was rewritten keeping the date of the
        text it replaced, which dates new prose as old."""
        _record_build(FIRST_BUILD)
        self.commit({"7": _prose("7"), "8": _prose("8")})
        _record_build(SECOND_BUILD)

        self.commit({"7": _prose("7", " Rewritten.")}, name="second.json")

        self.assertEqual(_authored(config.SUMMARIES_PATH), {"7": SECOND_STAMP, "8": FIRST_STAMP})

    def test_re_authoring_where_no_build_date_is_recorded_drops_the_old_date(self):
        """The break: keeping the old date when no new one can be read. The text is
        new, so the old date is wrong, and absent is the honest answer."""
        _record_build(FIRST_BUILD)
        self.commit({"7": _prose("7"), "8": _prose("8")})
        _record_build(None)

        self.commit({"7": _prose("7", " Rewritten.")}, name="second.json")

        self.assertEqual(_authored(config.SUMMARIES_PATH), {"8": FIRST_STAMP})

    def test_without_provenance_a_fresh_summary_is_left_undated(self):
        """The break: a date invented from somewhere other than the build - the
        clock, or an empty string a reader would test as present."""
        _record_build(None)
        self.commit({"7": _prose("7")})

        self.assertIsNone(_authored(config.SUMMARIES_PATH))

    def test_a_legacy_summary_stays_undated(self):
        """The break: dating a summary written before this change by the build that
        next touches the file. Nothing records when it was written, so nothing may
        say."""
        io.write_json(config.SUMMARIES_PATH, {"7": _prose("7")}, indent=1)
        _record_build(SECOND_BUILD)

        self.commit({"8": _prose("8")}, name="second.json")

        self.assertEqual(_authored(config.SUMMARIES_PATH), {"8": SECOND_STAMP})


class CarriedSummariesKeepTheirDate(_Store):
    """`summaries remap` moves prose to a new id; the date it was written moves with it."""

    def commit_and_recluster(self, old: dict, new: dict) -> None:
        _snapshot(old | _BACKGROUND)
        _write_graph(old | _BACKGROUND)
        _record_build(FIRST_BUILD)
        prose = {cid: _prose(cid) for cid in old | _BACKGROUND}
        self.assertEqual(_merge(self.root, prose, "first.json"), 0, "the fixture did not merge")
        _record_build(SECOND_BUILD)
        _write_graph(new | _BACKGROUND)

    def test_an_exact_carry_keeps_the_original_date(self):
        """The defect #485 names: a carried summary taking the date of the build
        that carried it, so prose written in March reads as written in May."""
        self.commit_and_recluster({"7": ["a", "b", "c"]}, {"42": ["a", "b", "c"]})

        self.assertEqual(_quiet(summaries.remap), 0)

        authored = _authored(config.SUMMARIES_PATH)
        self.assertNotIn("7", authored)
        self.assertEqual(authored["42"], FIRST_STAMP)
        self.assertEqual({stamp["authored_commit"] for stamp in authored.values()}, {"b" * 40})

    def test_an_overlap_carry_keeps_the_original_date(self):
        """The same break under the older tolerance: prose carried onto a cluster
        that grew is still the prose written for the old one."""
        old = {"7": [f"n{i}" for i in range(5)]}
        new = {"42": [f"n{i}" for i in range(5)] + [f"u{i}" for i in range(15)]}
        self.commit_and_recluster(old, new)

        self.assertEqual(_quiet(lambda: summaries.remap(carry=summaries.CARRY_OVERLAP)), 0)

        self.assertEqual(_authored(config.SUMMARIES_PATH)["42"], FIRST_STAMP)

    def test_the_block_is_ordered_by_community_id_after_a_remap(self):
        """The break: the block written in the order the remap resolved its
        targets, which follows the old ids rather than the new ones - so the same
        summaries would be written in a different order after each re-cluster."""
        self.commit_and_recluster(
            {"7": ["a", "b", "c"], "8": ["d", "e", "f"]},
            {"2000": ["a", "b", "c"], "3": ["d", "e", "f"]},
        )

        self.assertEqual(_quiet(summaries.remap), 0)

        carried = [
            cid for cid in _authored(config.SUMMARIES_PATH) if int(cid) < 1000 or cid == "2000"
        ]
        self.assertEqual(carried, ["3", "2000"])

    def test_a_carried_legacy_summary_stays_undated(self):
        """The break: a carry that dates what it moves when the source had no
        date - the remap is not authoring, so it has no date of its own to give."""
        _snapshot({"7": ["a", "b", "c"]} | _BACKGROUND)
        _write_graph({"42": ["a", "b", "c"]} | _BACKGROUND)
        legacy = {cid: _prose(cid) for cid in {"7": 0} | _BACKGROUND}
        io.write_json(config.SUMMARIES_PATH, legacy, indent=1)
        _record_build(SECOND_BUILD)

        self.assertEqual(_quiet(summaries.remap), 0)

        document = json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(document["42"], _prose("7"))
        self.assertNotIn("authored", document[io.SUMMARIES_METADATA_KEY])


# --- topic briefs and deep dives ------------------------------------------------


class _RenderedLayer(_Store):
    """The briefs and dives stages re-render every entry from Markdown on each run,
    so 'authored' means the rendered text changed, and only then."""

    ENTRY = ""

    def layer_path(self) -> Path:
        raise NotImplementedError

    def write_source(self, text: str) -> None:
        raise NotImplementedError

    def run_stage(self) -> int:
        raise NotImplementedError

    def build(self, build: dict | None) -> None:
        _record_build(build)
        self.assertEqual(_quiet(self.run_stage), 0, "the stage refused the fixture")

    def test_a_fresh_entry_takes_the_build_date(self):
        self.write_source(EVIDENCE)
        self.build(FIRST_BUILD)

        self.assertEqual(_authored(self.layer_path()), {self.ENTRY: FIRST_STAMP})

    def test_an_unchanged_entry_keeps_its_date(self):
        """The break: the stage rewrites every entry on every run, so dating what it
        wrote rather than what changed would re-date every brief each build."""
        self.write_source(EVIDENCE)
        self.build(FIRST_BUILD)

        self.build(SECOND_BUILD)

        self.assertEqual(_authored(self.layer_path()), {self.ENTRY: FIRST_STAMP})

    def test_a_rewritten_entry_takes_the_new_build_date(self):
        self.write_source(EVIDENCE)
        self.build(FIRST_BUILD)
        self.write_source("Rewritten by its author. " + EVIDENCE)

        self.build(SECOND_BUILD)

        self.assertEqual(_authored(self.layer_path()), {self.ENTRY: SECOND_STAMP})

    def test_a_legacy_entry_stays_undated(self):
        """The break: an entry from before this change, re-rendered unchanged,
        dated by the build that happened to re-render it."""
        self.write_source(EVIDENCE)
        self.build(None)
        self.assertIsNone(_authored(self.layer_path()))

        self.build(SECOND_BUILD)

        self.assertIsNone(_authored(self.layer_path()))


class BriefsAreDated(_RenderedLayer):
    ENTRY = "payments"

    def setUp(self):
        super().setUp()
        config.TOPICS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.TOPICS_CONFIG_PATH.write_text("payments | Payments | pay\n", encoding="utf-8")
        config.TOPICS_DOCS_DIR.mkdir(parents=True)

    def layer_path(self) -> Path:
        return config.TOPICS_BRIEFS_PATH

    def write_source(self, text: str) -> None:
        (config.TOPICS_DOCS_DIR / "payments.md").write_text(
            f"# Payments\n\n{text}", encoding="utf-8"
        )

    def run_stage(self) -> int:
        return briefs.merge()


class DivesAreDated(_RenderedLayer):
    ENTRY = "repo-one"
    SHA = "d" * 40

    def setUp(self):
        super().setUp()
        config.DEEPDIVES_INPUT_DIR.mkdir(parents=True)
        config.DEEPDIVES_DOCS_DIR.mkdir(parents=True)
        io.write_json(
            config.DEEPDIVES_INPUT_DIR / f"{self.ENTRY}-input.json",
            {"repo": self.ENTRY, "provenance": {"sha": self.SHA}},
        )

    def layer_path(self) -> Path:
        return config.DEEPDIVES_PATH

    def write_source(self, text: str) -> None:
        (config.DEEPDIVES_DOCS_DIR / f"{self.ENTRY}.md").write_text(
            f"# Deep dive\n\nMeasured at `{self.SHA[:8]}`.\n\n{text}", encoding="utf-8"
        )

    def run_stage(self) -> int:
        return dives.merge()


del _RenderedLayer  # a base, not a test case of its own


# --- the manifest ---------------------------------------------------------------


class TheManifestRecordsEachLayersRange(_Store):
    def manifest(self) -> dict:
        config.configure(STORE_ID="example-store")
        return store_manifest.compute(self.root)

    def test_the_range_is_the_oldest_and_newest_authored_date(self):
        """The break: no range, or a range taken by comparing dates as text. The
        `+01:00` date sorts last as text, and as an instant (06:00Z) it is the
        newest of none."""
        io.write_json(
            config.SUMMARIES_PATH,
            {
                "1": "One.",
                "2": "Two.",
                "3": "Three.",
                io.SUMMARIES_METADATA_KEY: {
                    "authored": {
                        "1": {"authored_as_of": "2026-03-01T07:00:00+01:00"},
                        "2": {"authored_as_of": "2026-03-01T06:30:00+00:00"},
                        "3": {"authored_as_of": "2026-02-28T23:00:00+00:00"},
                    }
                },
            },
        )

        layer = self.manifest()["layers"]["summaries"]

        self.assertEqual(
            layer["authored_as_of"],
            {"oldest": "2026-02-28T23:00:00+00:00", "newest": "2026-03-01T06:30:00+00:00"},
        )

    def test_a_layer_with_no_dated_entry_has_no_range(self):
        """The break: a range invented for a layer whose entries all predate this
        change, or for an absent layer. Null says the layer cannot be dated."""
        io.write_json(
            config.TOPICS_BRIEFS_PATH,
            {"payments": {"html": "<p>Brief.</p>"}, io.SUMMARIES_METADATA_KEY: {}},
        )

        layers = self.manifest()["layers"]

        self.assertIsNone(layers["topics"]["authored_as_of"])
        self.assertIsNone(layers["deep_dives"]["authored_as_of"])
        self.assertIsNone(layers["summaries"]["authored_as_of"])

    def test_the_range_is_read_from_what_the_stages_wrote(self):
        """End to end: briefs written in one build and re-dated by none since."""
        config.TOPICS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.TOPICS_CONFIG_PATH.write_text("payments | Payments | pay\n", encoding="utf-8")
        config.TOPICS_DOCS_DIR.mkdir(parents=True)
        (config.TOPICS_DOCS_DIR / "payments.md").write_text(EVIDENCE, encoding="utf-8")
        _record_build(FIRST_BUILD)
        self.assertEqual(_quiet(briefs.merge), 0)
        _record_build(SECOND_BUILD)
        self.assertEqual(_quiet(briefs.merge), 0)

        manifest = self.manifest()

        self.assertEqual(
            manifest["layers"]["topics"]["authored_as_of"],
            {"oldest": FIRST_STAMP["authored_as_of"], "newest": FIRST_STAMP["authored_as_of"]},
        )
        # The store's sources moved on; the briefs did not.
        self.assertEqual(manifest["sources_as_of"], SECOND_STAMP["authored_as_of"])


# --- determinism -----------------------------------------------------------------


class TwoRunsGiveIdenticalBytes(SettingsIsolated):
    """CLAUDE.md 1.3, over every file this change writes into."""

    def build_once(self, root: Path) -> dict[str, bytes]:
        config.configure(root=str(root), STORE_ID="example-store")
        (root / "knowledge" / "summaries").mkdir(parents=True)
        (root / "graphify-out").mkdir(parents=True)
        config.TOPICS_CONFIG_PATH.parent.mkdir(parents=True)
        config.TOPICS_CONFIG_PATH.write_text(
            "payments | Payments | pay\nwelsh | Welsh | cymraeg\n", encoding="utf-8"
        )
        config.TOPICS_DOCS_DIR.mkdir(parents=True)
        for slug in ("payments", "welsh"):
            (config.TOPICS_DOCS_DIR / f"{slug}.md").write_text(EVIDENCE + slug, encoding="utf-8")
        config.DEEPDIVES_INPUT_DIR.mkdir(parents=True)
        config.DEEPDIVES_DOCS_DIR.mkdir(parents=True)
        for repo in ("repo-two", "repo-one"):
            io.write_json(config.DEEPDIVES_INPUT_DIR / f"{repo}-input.json", {"repo": repo})
            (config.DEEPDIVES_DOCS_DIR / f"{repo}.md").write_text(EVIDENCE + repo, encoding="utf-8")

        _record_build(FIRST_BUILD)
        self.assertEqual(_merge(root, {"9": _prose("9"), "10": _prose("10")}, "a.json"), 0)
        self.assertEqual(_quiet(briefs.merge), 0)
        self.assertEqual(_quiet(dives.merge), 0)
        _record_build(SECOND_BUILD)
        self.assertEqual(_merge(root, {"7": _prose("7"), "9": _prose("9", " New.")}, "b.json"), 0)
        self.assertEqual(_quiet(briefs.merge), 0)
        self.assertEqual(_quiet(dives.merge), 0)
        manifest = store_manifest.compute(root)
        return {
            "summaries": config.SUMMARIES_PATH.read_bytes(),
            "briefs": config.TOPICS_BRIEFS_PATH.read_bytes(),
            "dives": config.DEEPDIVES_PATH.read_bytes(),
            "manifest": json.dumps(manifest, sort_keys=True).encode("utf-8"),
        }

    def test_two_builds_over_one_input_write_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = self.build_once(Path(one))
            second = self.build_once(Path(two))

        self.assertEqual(first, second)
        # And the run dated something, so the comparison is not over two empty blocks.
        self.assertIn(b"authored_as_of", first["summaries"])
        self.assertIn(b"authored_as_of", first["briefs"])
        self.assertIn(b"authored_as_of", first["dives"])


if __name__ == "__main__":
    unittest.main()
