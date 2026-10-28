"""Which store the server serves, and that no path leaves it.

Every test builds from the synthetic store (fixture_store/DESIGN.md 1 and 9) and
points XDG_CONFIG_HOME and `home` at temporary directories: nothing here reads
the real home.
"""

import argparse
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fixture_store.build import MANIFEST, PROVENANCE, build
from knowledgestore.server import manifest
from knowledgestore.server.manifest import Refused
from knowledgestore.server.locate import (
    AmbiguousStoreId,
    NotFound,
    OutsideStore,
    contained,
    resolve,
)


def args(store: str | None = None, store_id: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(store=store, store_id=store_id)


def set_store_id(root: Path, store_id: str) -> None:
    path = root / MANIFEST
    document = json.loads(path.read_text(encoding="utf-8"))
    document["store"]["id"] = store_id
    path.write_text(json.dumps(document), encoding="utf-8")


class LocateTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()

    def store(self, name: str, **kwargs) -> Path:
        return build(self.tmp / name, **kwargs)

    def write_locations(self, *lines: str) -> None:
        listing = self.home / ".config" / "knowledge-store" / "locations"
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        return dict(extra)


class PrecedenceTest(LocateTestCase):
    def setUp(self):
        super().setUp()
        self.explicit = self.store("explicit", index="none")
        self.from_env = self.store("from-env", index="none")
        self.listed = self.store("listed", index="none")
        self.plugin = self.store("plugin", index="none")
        self.write_locations(str(self.listed))
        self.everything = self.env(
            KNOWLEDGE_STORE_PATH=str(self.from_env), CLAUDE_PLUGIN_ROOT=str(self.plugin)
        )

    def test_each_rule_wins_over_the_ones_below_it(self):
        """Catches: a resolver that checks the rules in another order, so a
        container's environment variable overrides an explicit --store, or a
        plugin's own bundled store overrides the asker's clone."""
        got = resolve(
            args(store=str(self.explicit), store_id="example-store"), self.everything, self.home
        )
        self.assertEqual((got.root, got.source), (self.explicit, "--store"))

        got = resolve(args(store_id="example-store"), self.everything, self.home)
        self.assertEqual((got.root, got.source), (self.from_env, "KNOWLEDGE_STORE_PATH"))

        del self.everything["KNOWLEDGE_STORE_PATH"]
        got = resolve(args(store_id="example-store"), self.everything, self.home)
        self.assertEqual((got.root, got.source), (self.listed, "--store-id"))

        got = resolve(args(), self.everything, self.home)
        self.assertEqual((got.root, got.source), (self.plugin, "CLAUDE_PLUGIN_ROOT"))

    def test_a_plugin_root_without_a_manifest_does_not_count(self):
        """Catches: CLAUDE_PLUGIN_ROOT being taken whenever it is set. A plugin
        that ships no store sets it too, and would be served as an empty store."""
        bare = self.tmp / "bare-plugin"
        bare.mkdir()
        given, env = args(), self.env(CLAUDE_PLUGIN_ROOT=str(bare))
        with self.assertRaises(NotFound):
            resolve(given, env, self.home)

    def test_the_root_is_realpath_resolved_once(self):
        """Catches: a root left as the symlink the asker typed, so every later
        containment check compares against a path no resolved file can sit under."""
        link = self.tmp / "link-to-store"
        link.symlink_to(self.explicit)
        got = resolve(args(store=str(link)), {}, self.home)
        self.assertEqual(got.root, self.explicit)


class PluginRootTest(LocateTestCase):
    def setUp(self):
        super().setUp()
        self.plugin = self.store("plugin", index="none")

    def test_a_plugin_root_holding_the_requested_store_is_served(self):
        """Catches: a plugin root that ships the very store --store-id names being
        refused because the asker has no clone listed."""
        got = resolve(
            args(store_id="example-store"), self.env(CLAUDE_PLUGIN_ROOT=str(self.plugin)), self.home
        )
        self.assertEqual((got.root, got.source), (self.plugin, "CLAUDE_PLUGIN_ROOT"))

    def test_a_plugin_root_holding_another_store_is_refused_naming_both_ids(self):
        """Catches: serving a different store than the one asked for. --store-id
        example-store with a plugin bundling other-store would answer questions
        about the wrong estate, silently."""
        set_store_id(self.plugin, "other-store")
        given = args(store_id="example-store")
        env = self.env(CLAUDE_PLUGIN_ROOT=str(self.plugin))
        with self.assertRaises(NotFound) as caught:
            resolve(given, env, self.home)
        self.assertIn(
            "CLAUDE_PLUGIN_ROOT holds store 'other-store', not 'example-store'",
            str(caught.exception),
        )

    def test_without_a_store_id_any_plugin_root_store_counts(self):
        """Catches: the id check applied when no id was asked for, so a plugin that
        bundles its store and passes no --store-id is refused."""
        set_store_id(self.plugin, "other-store")
        got = resolve(args(), self.env(CLAUDE_PLUGIN_ROOT=str(self.plugin)), self.home)
        self.assertEqual(got.root, self.plugin)


class NotFoundTest(LocateTestCase):
    def test_the_message_names_all_four_sources(self):
        """Catches: a refusal that says only "no store" and leaves the asker to
        guess which of the four ways of naming one they had not used."""
        given, env = args(store_id="example-store"), self.env()
        with self.assertRaises(NotFound) as caught:
            resolve(given, env, self.home)
        message = str(caught.exception)
        for source in ("--store", "KNOWLEDGE_STORE_PATH", "--store-id", "CLAUDE_PLUGIN_ROOT"):
            self.assertIn(source, message)
        self.assertEqual(len(caught.exception.checked), 4)
        self.assertIn(str(self.home / ".config" / "knowledge-store" / "locations"), message)


class LocationsFileTest(LocateTestCase):
    def test_the_id_is_matched_against_the_manifest_not_the_directory_name(self):
        """Catches: a lookup by directory name. The clone called `example-store`
        holds another store (id `other-store`); the one called `decoy` is the
        store whose id is `example-store`, and must be the answer."""
        namesake = self.store("example-store", index="none")
        set_store_id(namesake, "other-store")
        decoy = self.store("decoy", index="none")
        self.write_locations(str(namesake), str(decoy))
        got = resolve(args(store_id="example-store"), self.env(), self.home)
        self.assertEqual(got.root, decoy)

    def test_comments_blank_lines_and_vanished_paths_are_skipped(self):
        """Catches: a reader that treats a comment, a blank line or a clone that
        has since been deleted as an entry, and fails or stops at it."""
        clone = self.store("clone", index="none")
        self.write_locations(
            "# the clone I use",
            "",
            "   ",
            str(self.tmp / "deleted-clone"),
            "relative/path",
            str(clone),
        )
        got = resolve(args(store_id="example-store"), self.env(), self.home)
        self.assertEqual(got.root, clone)

    def test_two_clones_with_one_id_are_refused_naming_both(self):
        """Catches: silently serving whichever clone is listed first. Two clones
        of one store can be at different commits, and the answer would depend on
        file order the asker cannot see (#454: refuse and name both)."""
        first = self.store("first", index="none")
        second = self.store("second", index="none")
        self.write_locations(str(first), str(second))
        given, env = args(store_id="example-store"), self.env()
        with self.assertRaises(AmbiguousStoreId) as caught:
            resolve(given, env, self.home)
        self.assertEqual(caught.exception.paths, [first, second])
        self.assertIn(str(first), str(caught.exception))
        self.assertIn(str(second), str(caught.exception))

    def test_one_clone_listed_twice_is_not_a_clash(self):
        """Catches: refusing a store because the same clone appears twice, once
        through a symlinked path."""
        clone = self.store("clone", index="none")
        link = self.tmp / "clone-link"
        link.symlink_to(clone)
        self.write_locations(str(clone), str(link), str(clone))
        got = resolve(args(store_id="example-store"), self.env(), self.home)
        self.assertEqual(got.root, clone)

    def test_a_clone_whose_manifest_cannot_be_read_is_skipped(self):
        """Catches: one half-pulled clone with a corrupt manifest making every
        other listed store unfindable."""
        broken = self.store("broken", index="none")
        (broken / MANIFEST).write_text("{not json", encoding="utf-8")
        good = self.store("good", index="none")
        self.write_locations(str(broken), str(good))
        got = resolve(args(store_id="example-store"), self.env(), self.home)
        self.assertEqual(got.root, good)

    def test_the_file_is_read_from_the_home_config_when_xdg_is_unset(self):
        """Catches: ignoring the documented default location, so a plugin that
        relies on ~/.config/knowledge-store/locations never finds its clone."""
        clone = self.store("clone", index="none")
        listing = self.home / ".config" / "knowledge-store" / "locations"
        listing.parent.mkdir(parents=True)
        listing.write_text(f"{clone}\n", encoding="utf-8")
        got = resolve(args(store_id="example-store"), {}, self.home)
        self.assertEqual(got.root, clone)

    def test_xdg_config_home_does_not_move_the_file(self):
        """Catches: honouring XDG_CONFIG_HOME. The skill appends to ~/.config whatever
        it says, so a reader looking in XDG finds nothing (an empty XDG listing here
        names a clone with another id; only the ~/.config one matches)."""
        right = self.store("right", index="none")
        self.write_locations(str(right))
        elsewhere = self.tmp / "xdg" / "knowledge-store"
        elsewhere.mkdir(parents=True)
        wrong = self.store("wrong", index="none")
        set_store_id(wrong, "wrong-store")
        (elsewhere / "locations").write_text(f"{wrong}\n", encoding="utf-8")
        got = resolve(
            args(store_id="example-store"),
            self.env(XDG_CONFIG_HOME=str(self.tmp / "xdg")),
            self.home,
        )
        self.assertEqual(got.root, right)

    def test_the_refusal_counts_skipped_entries_and_says_why(self):
        """Catches: a NotFound that hides why a listed clone was ignored, leaving
        the asker with a locations file that looks right and finds nothing."""
        self.write_locations("# note", "relative/path", str(self.tmp / "deleted-clone"))
        given, env = args(store_id="example-store"), self.env()
        with self.assertRaises(NotFound) as caught:
            resolve(given, env, self.home)
        message = str(caught.exception)
        self.assertIn("2 entries skipped", message)
        self.assertIn("relative/path (not absolute)", message)
        self.assertIn(f"{self.tmp / 'deleted-clone'} (not a directory)", message)


class ContainmentTest(LocateTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.store("store", index="none")
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "secret.json").write_text("{}", encoding="utf-8")

    def test_a_path_inside_the_store_is_returned_resolved(self):
        """Catches: a containment check that rejects ordinary store paths, or
        returns the unresolved form."""
        self.assertEqual(contained(self.root, PROVENANCE), self.root / PROVENANCE)

    def test_dot_dot_cannot_leave_the_store(self):
        """Catches: a string-prefix check done before `..` is resolved, which
        lets `knowledge/../../outside/secret.json` through."""
        with self.assertRaises(OutsideStore):
            contained(self.root, "knowledge/../../outside/secret.json")

    def test_a_symlinked_file_pointing_out_of_the_store_is_refused(self):
        """Catches: containment checked on the joined path rather than its
        realpath, so a symlink committed to a store reads any file the process can."""
        target = self.root / PROVENANCE
        target.unlink()
        target.symlink_to(self.outside / "secret.json")
        with self.assertRaises(OutsideStore):
            contained(self.root, PROVENANCE)

    def test_a_symlinked_directory_pointing_out_of_the_store_is_refused(self):
        """Catches: only the final path component being resolved, so a symlinked
        directory in the middle of the path escapes."""
        shutil.rmtree(self.root / "knowledge" / "topics")
        (self.root / "knowledge" / "topics").symlink_to(self.outside)
        with self.assertRaises(OutsideStore):
            contained(self.root, "knowledge/topics/secret.json")

    def test_a_sibling_directory_sharing_the_root_prefix_is_outside(self):
        """Catches: a `startswith(root)` check, which takes `store-evil` to be
        inside `store`."""
        evil = self.tmp / "store-evil"
        evil.mkdir()
        (self.root / "link").symlink_to(evil)
        with self.assertRaises(OutsideStore):
            contained(self.root, "link")

    def test_a_manifest_symlinked_out_of_the_store_is_not_read(self):
        """Catches: manifest.load following its own file out of the store, or
        letting a second exception type escape start-up instead of Refused."""
        real = self.outside / "store-manifest.json"
        (self.root / MANIFEST).replace(real)
        (self.root / MANIFEST).symlink_to(real)
        with self.assertRaises(Refused) as caught:
            manifest.load(self.root)
        self.assertIn("a manifest outside the store", str(caught.exception))
        # Where it resolves to is never said, on stderr or anywhere (SECURITY.md).
        self.assertNotIn(str(real), str(caught.exception))

    def test_a_listed_clone_whose_manifest_escapes_is_not_matched(self):
        """Catches: a --store-id lookup reading a manifest outside the clone, so a
        planted symlink claims an id it does not hold."""
        real = self.outside / "store-manifest.json"
        (self.root / MANIFEST).replace(real)
        (self.root / MANIFEST).symlink_to(real)
        self.write_locations(str(self.root))
        given, env = args(store_id="example-store"), self.env()
        with self.assertRaises(NotFound):
            resolve(given, env, self.home)


if __name__ == "__main__":
    unittest.main()
