"""The manifest's shape cannot change without a format bump.

`store_manifest.FORMAT` is typed by hand, and a reader such as the MCP server
trusts it. Nothing else notices a layer or a field added, removed, renamed or
retyped while `FORMAT` stays put, so a reader would go on reading a store whose
shape had changed under it. This module computes the shape of the manifest
`compute` really produces and holds it against two records: `FORMAT_SHAPE`, the
hash recorded beside `FORMAT`, and `manifest_format_shapes.json`, the shape
recorded under every format ever released.

The shape is every key path of the manifest with the type of what it holds, plus
each library layer's kind. Values never enter it, so a different store or a
different day gives the same shape. It is measured on a representative store
holding every library layer, one extra layer of each kind and one that git
ignores, so every optional field is present.

Updating the record is a decision and not a regeneration: the failure message
says which bump the change needs and prints the entry to add.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated
from knowledgestore import config, store_manifest
from knowledgestore.git_env import git_env

RECORD = Path(__file__).with_name("manifest_format_shapes.json")


def _type_of(value) -> str:
    if value is None:
        return "null"
    # bool before int: a bool is an int in Python, and the manifest means the difference.
    for kind, name in ((bool, "bool"), (int, "int"), (str, "str")):
        if isinstance(value, kind):
            return name
    if isinstance(value, list):
        return "list[" + "|".join(sorted({_type_of(item) for item in value})) + "]"
    raise TypeError(f"the manifest holds a {type(value).__name__}, which the shape cannot name")


def _flatten(prefix: str, value, found: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else key, item, found)
    else:
        found.setdefault(prefix, set()).add(_type_of(value))


def shape_lines(manifest: dict) -> list[str]:
    """The manifest's shape as sorted `key.path: type` lines, never its values.

    An extra layer's name is the store's own, so every extra layer is read under
    `extra_layers.*`: a store with more of them has the same shape.
    """
    found: dict[str, set[str]] = {}
    _flatten("", {k: v for k, v in manifest.items() if k != "extra_layers"}, found)
    for entry in manifest["extra_layers"].values():
        _flatten("extra_layers.*", entry, found)
    lines = [f"{path}: {'|'.join(sorted(types))}" for path, types in found.items()]
    lines += [f"layer-kind.{layer.name}: {layer.kind}" for layer in store_manifest.LAYERS]
    return sorted(lines)


def shape_hash(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def required_bump(recorded: list[str], computed: list[str]) -> str | None:
    """`minor` for additions alone, `major` for any removal, rename or type change."""
    before = dict(line.split(": ", 1) for line in recorded)
    after = dict(line.split(": ", 1) for line in computed)
    if any(after.get(path) != kind for path, kind in before.items()):
        return "major"
    return "minor" if set(after) - set(before) else None


def _as_tuple(key: str) -> tuple[int, int]:
    major, minor = key.split(".")
    return int(major), int(minor)


def _entry_text(fmt: tuple[int, int], lines: list[str]) -> str:
    entry = {f"{fmt[0]}.{fmt[1]}": {"hash": shape_hash(lines), "shape": lines}}
    return json.dumps(entry, indent=2)


def _bump_problems(fmt, history: dict, lines: list[str]) -> list[str]:
    """What is wrong when FORMAT has no recorded shape: the bump is judged against
    the latest earlier format, then the missing entry is asked for."""
    earlier = [key for key in history if _as_tuple(key) < fmt]
    problems = []
    if earlier:
        previous = max(earlier, key=_as_tuple)
        needed = required_bump(history[previous]["shape"], lines)
        if needed == "major" and _as_tuple(previous)[0] == fmt[0]:
            problems.append(
                f"The shape changed since {previous} in a way that needs a major bump, and "
                f"FORMAT {fmt[0]}.{fmt[1]} keeps major {fmt[0]}."
            )
    problems.append(
        f"FORMAT {fmt[0]}.{fmt[1]} has no recorded shape. Add this entry to {RECORD.name}, "
        f"and set FORMAT_SHAPE to its hash:\n{_entry_text(fmt, lines)}"
    )
    return problems


def _changed_problems(fmt, recorded: dict, lines: list[str]) -> list[str]:
    diff = "\n".join(
        difflib.unified_diff(recorded["shape"], lines, "recorded", "computed", lineterm="")
    )
    needed = required_bump(recorded["shape"], lines)
    return [
        f"The manifest's shape changed while FORMAT stayed {fmt[0]}.{fmt[1]}. This change "
        f"needs a {needed} bump of FORMAT: minor for additions, major for removals, renames "
        "or type changes. Then add the new shape to "
        f"{RECORD.name} and set FORMAT_SHAPE to its hash.\n{diff}"
    ]


def judge(fmt, format_shape: str, history: dict, lines: list[str]) -> list[str]:
    """Everything wrong between the computed shape, FORMAT, FORMAT_SHAPE and the record."""
    key = f"{fmt[0]}.{fmt[1]}"
    problems = []
    if any(_as_tuple(recorded_key) > fmt for recorded_key in history):
        problems.append(f"FORMAT {key} is below a format the record already holds.")
    if key not in history:
        problems += _bump_problems(fmt, history, lines)
    elif history[key]["hash"] != shape_hash(lines):
        problems += _changed_problems(fmt, history[key], lines)
    if format_shape != shape_hash(lines):
        problems.append(
            f"FORMAT_SHAPE is {format_shape} but the computed shape hashes to "
            f"{shape_hash(lines)}. Update it with FORMAT, never alone."
        )
    return problems


def _touch(root: Path, rel: str, text: str = "{}") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def layer_path(name: str) -> str:
    return next(layer.paths[0] for layer in store_manifest.LAYERS if layer.name == name)


def representative_manifest(root: Path) -> dict:
    """`compute` over a real git store holding every layer and every optional field."""
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=git_env())
    for layer in store_manifest.LAYERS:
        rel = layer.paths[0]
        _touch(root, f"{rel}/entry.json" if layer.kind == "dir" else rel)
    _touch(
        root,
        "knowledge/telemetry.json",
        json.dumps(
            {
                "measurements": {
                    "source_urls.linked": 3,
                    "intent.repositories_without_reach": 0,
                    "deployments.pairs": 2,
                }
            }
        ),
    )
    _touch(
        root,
        "knowledge/provenance.json",
        json.dumps({"repositories": {"a": {"sha": "1", "committed": "2026-01-01T00:00:00+00:00"}}}),
    )
    _touch(
        root,
        "config/store-extra-layers.json",
        json.dumps(
            {
                "journeys": {"path": "docs/journeys", "description": "J.", "kind": "answers"},
                "scratch": {"path": "scratch", "description": "Ignored."},
            }
        ),
    )
    _touch(
        root,
        "knowledge/relationships/repository-dependencies.json",
        json.dumps({"format": {"layer": "relationships", "major": 1}, "unresolved_conflicts": {}}),
    )
    # A written layer with one dated entry, so its `authored_as_of` range is an
    # object rather than null.
    dated = {"_metadata": {"authored": {"1": {"authored_as_of": "2026-01-01T00:00:00+00:00"}}}}
    for layer in store_manifest.AUTHORED_LAYERS:
        _touch(root, layer_path(layer), json.dumps(dated))
    _touch(root, "docs/journeys/apply.md", "# Apply")
    _touch(root, "scratch/note.md", "n")
    _touch(root, ".gitignore", "scratch/\n")
    config.configure(STORE_ID="example-store")
    return store_manifest.compute(root)


def _recorded() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


class TheManifestShapeIsGuarded(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lines = shape_lines(representative_manifest(Path(tmp.name)))
        self.history = _recorded()

    def test_the_shape_matches_the_format_it_is_recorded_under(self):
        """Breaks if a layer or field is added, removed, renamed or retyped without
        FORMAT being bumped and FORMAT_SHAPE and the record being updated with it:
        a reader would trust a format that has changed under it."""
        problems = judge(
            store_manifest.FORMAT, store_manifest.FORMAT_SHAPE, self.history, self.lines
        )
        self.assertEqual(problems, [], "\n\n".join(problems))

    def test_every_recorded_shape_hashes_to_its_recorded_hash(self):
        """Breaks if a record entry is hand-edited so its shape and hash disagree,
        which would let a changed shape pass as an unchanged one."""
        for key, entry in self.history.items():
            with self.subTest(format=key):
                self.assertEqual(entry["shape"], sorted(entry["shape"]))
                self.assertEqual(shape_hash(entry["shape"]), entry["hash"])

    def test_the_shape_names_what_a_reader_depends_on(self):
        """Breaks if the shape stops reading the manifest (an empty or partial
        description would make every comparison above pass vacuously)."""
        for expected in (
            "format.major: int",
            "layers.graph.digest: str",
            "layers.graph.source_urls: bool",
            "layers.relationships.recognised: bool",
            "layers.relationships.unresolved_conflicts: int",
            "layers.deployments.present: bool",
            "extra_layers.*.local_only: bool",
            "extra_layers.*.kind: str",
            "layer-kind.history: dir",
            "layer-kind.graph: file",
            "sources_as_of: str",
            "layers.summaries.authored_as_of.newest: str",
            "layers.topics.authored_as_of.oldest: str",
        ):
            self.assertIn(expected, self.lines)

    # The three self-checks below show the guard can fail, in the same run. Each is
    # covered by the mutation entry 'store manifest: a field added without a format
    # bump' (tests/mutation_gate.py) for the real comparison; these prove the
    # comparison discriminates, so a judge returning nothing cannot pass.

    def guard_says(self, lines: list[str]) -> str:
        return "\n".join(
            judge(store_manifest.FORMAT, store_manifest.FORMAT_SHAPE, self.history, lines)
        )

    def test_a_layer_added_to_a_copy_of_the_shape_needs_a_minor_bump(self):
        """Breaks if the guard lets an added layer through, or demands a major bump
        for it: readers of the old format still read the new one."""
        added = [line.replace("layers.graph.", "layers.example.") for line in self.lines]
        added = sorted(set(self.lines) | set(added) | {"layer-kind.example: file"})
        said = self.guard_says(added)
        self.assertEqual(required_bump(self.lines, added), "minor")
        self.assertIn("needs a minor bump", said)
        self.assertNotIn("needs a major bump", said)

    def test_a_renamed_field_needs_a_major_bump(self):
        """Breaks if a rename is judged an addition: an old reader looks for the
        old name and finds nothing."""
        renamed = sorted(
            line.replace("layers.graph.digest", "layers.graph.hash") for line in self.lines
        )
        self.assertEqual(required_bump(self.lines, renamed), "major")
        self.assertIn("needs a major bump", self.guard_says(renamed))

    def test_a_changed_type_needs_a_major_bump(self):
        """Breaks if a retyped field is let through: an old reader parses it as the
        type it used to be."""
        retyped = sorted(
            line.replace("layers.graph.digest: str", "layers.graph.digest: int")
            for line in self.lines
        )
        self.assertEqual(required_bump(self.lines, retyped), "major")
        self.assertIn("needs a major bump", self.guard_says(retyped))

    def test_an_unchanged_shape_needs_no_bump(self):
        """Breaks if the guard asks for a bump nothing called for, which would train
        people to bump without reading."""
        self.assertIsNone(required_bump(self.lines, list(self.lines)))

    def test_the_hash_alone_cannot_be_updated_without_a_bump(self):
        """Breaks if FORMAT_SHAPE and the changed shape are enough: the point of
        recording the format beside the shape is that the hash cannot be refreshed
        to silence the guard while FORMAT stays where it is."""
        changed = sorted(self.lines + ["layers.graph.added: str"])
        problems = judge(store_manifest.FORMAT, shape_hash(changed), self.history, changed)
        self.assertTrue(any("needs a minor bump" in problem for problem in problems))

    def test_a_bump_that_is_too_small_or_unrecorded_still_fails(self):
        """Breaks if a minor bump satisfies a removal, or a bumped FORMAT with no
        recorded shape passes."""
        removed = [line for line in self.lines if not line.startswith("layers.graph.digest")]
        major, minor = store_manifest.FORMAT
        minor_bumped = judge((major, minor + 1), shape_hash(removed), self.history, removed)
        self.assertTrue(any("needs a major bump" in problem for problem in minor_bumped))
        major_bumped = judge((major + 1, 0), shape_hash(removed), self.history, removed)
        self.assertEqual(len(major_bumped), 1)
        self.assertIn("has no recorded shape", major_bumped[0])

    def test_a_correct_bump_with_its_record_passes(self):
        """Breaks if the way forward the message describes does not itself pass:
        a minor bump for an addition, the entry recorded, FORMAT_SHAPE updated."""
        added = sorted(self.lines + ["layers.graph.added: str"])
        major, minor = store_manifest.FORMAT
        fmt = (major, minor + 1)
        history = {
            **self.history,
            f"{major}.{minor + 1}": {"hash": shape_hash(added), "shape": added},
        }
        self.assertEqual(judge(fmt, shape_hash(added), history, added), [])


if __name__ == "__main__":
    unittest.main()
