"""A store registers its own relationship kinds in `config/relationship-kinds.json`.

The registry's built-in kinds are code in this library; a store's kind is code in
the store, loaded from a file inside the store root and run with the builder's own
authority. These tests run the real stage over a real fixture store, so what they
assert is the layer on disk, not that a loader was called.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from test_build_relationships import GITHUB, TOKEN, _Store, _write  # noqa: E402
from knowledgestore import relationships  # noqa: E402

KIND = "event_subscription"

SCRIPT = '''\
"""A store's parser for `subscribe <topic>@<pin>` lines in `*.events` files."""

from pathlib import Path

from knowledgestore.relationships import Reference


def references(clone, rel, text):
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("explode"):
            raise RuntimeError("cannot read this subscription")
        if line.startswith("subscribe "):
            topic, _, pin = line[len("subscribe "):].partition("@")
            found.append(
                Reference(
                    kind="event_subscription",
                    target=topic,
                    chart="",
                    ref=pin,
                    rel=rel,
                    line=number,
                    text=line,
                )
            )
    return found


def publishes(clone):
    found = []
    for path in sorted(Path(clone).rglob("*.events")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("publish "):
                found.append((line[len("publish "):], str(path.relative_to(clone))))
    return found
'''

CONFIG = {
    KIND: {
        "module": "scripts/subscriptions.py",
        "references": "references",
        "publishes": "publishes",
        "globs": ["**/*.events"],
    }
}

ESTATE = {
    "topics": {"orders.events": "publish orders.created\n"},
    "billing": {"billing.events": "subscribe orders.created@v2\nsubscribe legacy.missing\n"},
}


class _StoreKinds(_Store):
    def estate(self, extra=None) -> None:
        """Only the clones named: the shared fixture estate would add its own kinds'
        references to every total asserted here."""
        for name, files in (extra or {}).items():
            _write(self.repos / name, files)

    def declare(self, config_body=None, script=SCRIPT) -> None:
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts" / "subscriptions.py").write_text(script, encoding="utf-8")
        (self.root / "config").mkdir(exist_ok=True)
        body = CONFIG if config_body is None else config_body
        (self.root / "config" / "relationship-kinds.json").write_text(
            json.dumps(body), encoding="utf-8"
        )

    def load(self):
        return relationships.load_store_kinds(
            self.root, self.root / "config" / "relationship-kinds.json"
        )

    def layer_dir(self) -> Path:
        return self.root / "knowledge" / "relationships"

    def read(self, name: str):
        return json.loads((self.layer_dir() / name).read_text(encoding="utf-8"))


class StoreKindLayerTest(_StoreKinds):
    def test_a_store_kind_produces_edges_unresolved_targets_and_an_origin(self):
        """Break caught: a registered kind loaded but never run, run but left out of
        `kinds.json`, or listed there as a built-in - so the manifest cannot tell a
        store's own vocabulary from the library's."""
        self.estate(ESTATE)
        self.declare()
        code, _, err = self.run_stage()
        self.assertEqual(code, 0, err)

        edges = self.read("by-repo/billing.json")["depends_on"]
        self.assertEqual(
            [(e["repository"], e["kinds"], e["refs_pinned"]) for e in edges if KIND in e["kinds"]],
            [("topics", {KIND: 1}, {"v2": 1})],
        )
        self.assertEqual(
            self.read("repository-dependencies.json")["unresolved_targets_outside_estate"][KIND],
            {"legacy.missing": {"references": 1, "referenced_by": ["billing"]}},
        )
        kinds = self.read("kinds.json")
        self.assertEqual(kinds[KIND]["origin"], "store")
        self.assertEqual(
            (kinds[KIND]["edges"], kinds[KIND]["references"], kinds[KIND]["unresolved"]),
            (1, 1, 1),
        )
        self.assertEqual(
            {name: body["origin"] for name, body in kinds.items() if name != KIND},
            {
                "helm_chart": "builtin",
                "maven_artifact": "builtin",
                "npm_package": "builtin",
                "terraform_module": "builtin",
            },
        )

    def test_a_store_without_the_file_has_only_the_built_in_kinds(self):
        """Break caught: a missing config file read as an error, which would stop the
        stage for every store that registers nothing."""
        self.estate(ESTATE)
        code, _, err = self.run_stage()
        self.assertEqual(code, 0, err)
        self.assertNotIn(KIND, self.read("kinds.json"))

    def test_store_kinds_load_in_name_order_whatever_the_file_order(self):
        """Break caught: kinds ordered as the file lists them, so reordering a JSON
        object reorders nothing visible until two kinds publish one target and the
        first wins (CLAUDE.md 1.3)."""
        self.declare({"zz_last": CONFIG[KIND], "aa_first": CONFIG[KIND]})
        self.assertEqual([k.name for k in self.load()], ["aa_first", "zz_last"])
        self.assertEqual({k.origin for k in self.load()}, {"store"})

    def test_two_stores_in_one_process_do_not_share_a_module(self):
        """Break caught: a fixed module name, so the second store's `subscriptions`
        is the first store's cached module and its kind runs the wrong code - and a
        store directory put on `sys.path`, which would let one store's `import x`
        resolve to another's file."""
        self.declare()
        first = self.load()[0]
        other = self.root / "other-store"
        (other / "scripts").mkdir(parents=True)
        (other / "config").mkdir()
        (other / "scripts" / "subscriptions.py").write_text(
            SCRIPT.replace("subscribe ", "listen "), encoding="utf-8"
        )
        (other / "config" / "relationship-kinds.json").write_text(
            json.dumps(CONFIG), encoding="utf-8"
        )
        before = list(sys.path)
        second = relationships.load_store_kinds(
            other, other / "config" / "relationship-kinds.json"
        )[0]
        self.assertEqual(sys.path, before)
        clone = self.root / "c"
        self.assertEqual(len(first.references(clone, "a.events", "subscribe t@1\n")), 1)
        self.assertEqual(len(second.references(clone, "a.events", "subscribe t@1\n")), 0)
        self.assertEqual(len(second.references(clone, "a.events", "listen t@1\n")), 1)


class RejectedDeclarationTest(_StoreKinds):
    def rejected(self, entry: dict, field: str) -> None:
        self.declare({KIND: {**CONFIG[KIND], **entry}})
        with self.assertRaisesRegex(relationships.UsageError, field) as caught:
            self.load()
        self.assertIn(KIND, str(caught.exception))

    def test_a_module_outside_the_store_is_refused_before_it_runs(self):
        """Break caught: containment missing, so a config naming a file elsewhere on
        the machine executes it. The marker proves the file was never imported."""
        marker = self.root / "ran"
        outside = self.root.parent / f"{self.root.name}-outside.py"
        outside.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        self.rejected({"module": str(outside)}, "module")
        self.assertFalse(marker.exists())

    def test_a_dotdot_path_is_refused_even_when_it_lands_inside(self):
        """Break caught: `..` resolved away before it is checked, so a path that
        climbs out and back in is accepted and the rule reads as advisory."""
        self.rejected({"module": "scripts/../scripts/subscriptions.py"}, "module")

    def test_a_symlink_that_escapes_the_store_is_refused(self):
        """Break caught: containment checked on the written path, not the real one,
        so a link inside the store reaches code outside it."""
        marker = self.root / "ran"
        outside = self.root.parent / f"{self.root.name}-linked.py"
        outside.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        self.declare()
        os.symlink(outside, self.root / "scripts" / "link.py")
        self.rejected({"module": "scripts/link.py"}, "module")
        self.assertFalse(marker.exists())

    def test_a_missing_module_file_is_refused(self):
        """Break caught: a typo in `module` surfacing as a bare ImportError."""
        self.rejected({"module": "scripts/nope.py"}, "module")

    def test_a_kind_cannot_shadow_a_built_in(self):
        """Break caught: a store replacing `npm_package`, so the layer's meaning of a
        built-in kind changes per store and a consumer cannot rely on the name."""
        self.declare({"npm_package": CONFIG[KIND]})
        with self.assertRaisesRegex(relationships.UsageError, "kind name") as caught:
            self.load()
        self.assertIn("npm_package", str(caught.exception))

    def test_a_bad_kind_name_is_refused(self):
        """Break caught: a name that cannot be a JSON key or a file stem safely
        (`Has Space`, `1st`) reaching `kinds.json`."""
        for bad in ("Has Space", "1st", "Upper", "a-b", "", "abc\n"):
            with self.subTest(bad=bad):
                self.declare({bad: CONFIG[KIND]})
                with self.assertRaisesRegex(relationships.UsageError, "kind name"):
                    self.load()

    def test_a_missing_function_is_refused(self):
        """Break caught: a misspelt function name found only when the first clone is
        scanned, after other stages have run."""
        self.rejected({"references": "nope"}, "references")
        self.rejected({"publishes": "nope"}, "publishes")

    def test_a_declaration_without_its_function_keys_is_refused(self):
        """Break caught: an absent `references` or `publishes` key read as `None` and
        failing later as a traceback, or as a kind that quietly reads nothing."""
        for key in ("references", "publishes"):
            with self.subTest(key=key):
                entry = {k: v for k, v in CONFIG[KIND].items() if k != key}
                self.declare({KIND: entry})
                with self.assertRaisesRegex(relationships.UsageError, key):
                    self.load()

    def test_every_declaration_is_checked_before_any_module_is_imported(self):
        """Break caught: modules imported as the file is read, so a bad second
        declaration is reported only after the first store module has already run."""
        marker = self.root / "ran"
        self.declare(
            {"aa_ok": CONFIG[KIND], "bb_bad": {**CONFIG[KIND], "globs": []}},
            script=f"open({str(marker)!r}, 'w').close()\n" + SCRIPT,
        )
        with self.assertRaisesRegex(relationships.UsageError, "globs"):
            self.load()
        self.assertFalse(marker.exists())

    def test_a_missing_or_malformed_field_is_refused(self):
        """Break caught: a missing `globs` read as 'match nothing', so a registered
        kind silently reports no edges."""
        self.rejected({"globs": []}, "globs")
        self.rejected({"globs": "**/*.events"}, "globs")
        self.rejected({"module": 7}, "module")


class StoreKindFailureTest(_StoreKinds):
    def test_a_raising_parser_exits_1_and_leaves_the_previous_layer_intact(self):
        """Break caught: a half-written layer after a parser fails - a new
        `kinds.json` beside an old `repository-dependencies.json` - or a failure
        that does not say which kind and which file."""
        self.estate(ESTATE)
        self.declare()
        code, _, err = self.run_stage()
        self.assertEqual(code, 0, err)
        before = self.snapshot()

        _write(self.repos / "billing", {"broken.events": "explode\n"})
        code, _, err = self.run_stage()
        self.assertEqual(code, 1)
        self.assertIn(KIND, err)
        self.assertIn("billing/broken.events", err)
        self.assertEqual(self.snapshot(), before)

    def test_a_script_that_fails_to_import_exits_1_naming_the_kind_and_file(self):
        """Break caught: a syntax error in the store's script escaping as a
        traceback instead of a refusal that names what to fix."""
        self.estate(ESTATE)
        self.declare(script="def references(:\n")
        code, _, err = self.run_stage()
        self.assertEqual(code, 1)
        self.assertIn(KIND, err)
        self.assertIn("scripts/subscriptions.py", err)
        self.assertFalse(self.layer_dir().exists())


class BadParserResultTest(_StoreKinds):
    def failing(self, override: str, *, expect: str) -> None:
        self.estate(ESTATE)
        self.declare(script=SCRIPT + override)
        code, _, err = self.run_stage()
        self.assertEqual(code, 1, err)
        self.assertIn(KIND, err)
        self.assertIn(expect, err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.layer_dir().exists())

    def test_a_reference_under_another_kinds_name_is_refused(self):
        """Break caught: a reference whose `kind` is not the registered name resolves
        against another kind's publishers - nothing - and vanishes with no message."""
        self.failing(
            "\n_real = references\n\n"
            "def references(clone, rel, text):\n"
            "    return [Reference(**{**r.__dict__, 'kind': 'typo'}) for r in _real(clone, rel, text)]\n",
            expect="billing/billing.events",
        )

    def test_a_chart_from_a_kind_other_than_helm_chart_is_refused_without_echoing_it(self):
        """Break caught: a store kind's `chart` becoming an edge key and being written
        unmasked - and the refusal itself repeating the value, which can be a secret."""
        self.estate(ESTATE)
        self.declare(
            script=SCRIPT + "\n_real = references\n\n"
            "def references(clone, rel, text):\n"
            "    return [Reference(**{**r.__dict__, 'chart': 'ghp_" + "x" * 36 + "'})"
            " for r in _real(clone, rel, text)]\n"
        )
        code, _, err = self.run_stage()
        self.assertEqual(code, 1)
        self.assertIn("chart", err)
        self.assertNotIn("x" * 36, err)
        self.assertFalse(self.layer_dir().exists())

    def test_a_wrong_return_type_is_refused_not_a_traceback(self):
        """Break caught: `None`, a dict or a bare Reference from `references`, or a
        malformed `publishes`, escaping as a traceback or being iterated into nonsense."""
        for body in (
            "None",
            "[{'a': 1}]",
            "Reference('event_subscription', 't', '', '', rel, 1, '')",
        ):
            with self.subTest(references=body):
                self.failing(
                    f"\ndef references(clone, rel, text):\n    return {body}\n",
                    expect="billing/billing.events",
                )
        for body in ("None", "5", "[('only-one',)]", "[(1, 'x')]"):
            with self.subTest(publishes=body):
                self.failing(f"\ndef publishes(clone):\n    return {body}\n", expect="publishes()")

    def test_a_refusal_names_the_kind_the_file_and_the_type_and_echoes_no_store_text(self):
        """Break caught: a refusal repeating what the store's parser returned or the
        text of what it raised - a pair, a kind name, an exception message - any of
        which can carry a credential the store read, printed where a build log keeps
        it."""
        cases = (
            (
                f"\ndef references(clone, rel, text):\n    raise RuntimeError({TOKEN!r})\n",
                ("billing/billing.events", "RuntimeError"),
            ),
            (
                "\n_real = references\n\n"
                "def references(clone, rel, text):\n"
                f"    return [Reference(**{{**r.__dict__, 'kind': {TOKEN!r}}})"
                " for r in _real(clone, rel, text)]\n",
                ("billing/billing.events",),
            ),
            (
                f"\ndef publishes(clone):\n    raise RuntimeError({TOKEN!r})\n",
                ("publishes()", "RuntimeError"),
            ),
            (
                f"\ndef publishes(clone):\n    return [({TOKEN!r}, 5)]\n",
                ("publishes()", "str", "int"),
            ),
        )
        for override, expected in cases:
            with self.subTest(override=override):
                self.estate(ESTATE)
                self.declare(script=SCRIPT + override)
                code, _, err = self.run_stage()
                self.assertEqual(code, 1, err)
                self.assertIn(KIND, err)
                for text in expected:
                    self.assertIn(text, err)
                self.assertNotIn(TOKEN, err)


class StoreKindMaskingTest(_StoreKinds):
    def test_a_credential_in_a_store_kinds_reference_reaches_no_file(self):
        """Break caught: store kinds exempt from the rules the built-ins follow, so a
        `user:token@` URL in a target, a token in a pin or a token-shaped value in an
        example line is committed in the layer."""
        script = SCRIPT.replace(
            'target=topic,\n                    chart="",\n                    ref=pin,',
            f'target="https://user:{TOKEN}@broker.example.test/" + topic,\n'
            f'                    chart="",\n                    ref=pin + "?access_token={TOKEN}",',
        ).replace("text=line,", f'text=line + " {GITHUB}",')
        self.assertNotEqual(script, SCRIPT)
        self.estate(ESTATE)
        self.declare(script=script)
        code, _, err = self.run_stage()
        self.assertEqual(code, 0, err)
        written = "".join(text for text in (v.decode() for v in self.snapshot().values()))
        self.assertIn("broker.example.test/orders.created", written)
        self.assertNotIn(TOKEN, written)
        self.assertNotIn(GITHUB, written)
        self.assertNotIn("user:", written)

    def test_a_chart_on_an_edge_key_is_masked_whatever_wrote_it(self):
        """Break caught: the chart, which keys an edge and is written, skipping the
        masking pins and targets get. A store kind cannot reach it (it is refused),
        so this drives `build` with a kind named `helm_chart`, as a defence in depth."""

        def references(clone, rel, text):
            return [
                relationships.Reference(
                    "helm_chart", "t", GITHUB, "", rel, 1, "x", resolved_repo="topics", org=""
                )
            ]

        self.estate(ESTATE)
        kind = relationships.Kind(
            "helm_chart", "builtin", ("**/*.events",), references, lambda clone: []
        )
        layer = self.build(kinds=(kind,))
        self.assertNotIn(GITHUB, json.dumps(layer.dependencies))
        self.assertNotIn(GITHUB, json.dumps(layer.by_repo))


class EveryKindReportsWhatItCannotResolveTest(_StoreKinds):
    def test_a_resolvable_and_an_unresolvable_reference_per_kind(self):
        """Break caught: a kind - built-in or a store's - whose unresolved reference
        goes missing from the output, so a dependency the store does not hold reads
        as no dependency. One estate holds, for every registered kind, one reference
        a clone provides and one nothing does."""
        self.estate(
            {
                "provider": {
                    "Chart.yaml": "name: shared\nversion: 1.0.0\n",
                    "pom.xml": (
                        "<project><groupId>com.example</groupId><artifactId>lib-a</artifactId>"
                        "<version>1</version></project>\n"
                    ),
                    "package.json": json.dumps({"name": "@example/kit"}),
                    "orders.events": "publish orders.created\n",
                },
                "tf-provider": {"main.tf": 'variable "x" {}\n'},
                "consumer": {
                    "main.tf": (
                        'module "in" {\n  source = "github.com/example-org/tf-provider"\n}\n'
                        'module "out" {\n  source = "hashicorp/outside/aws"\n}\n'
                    ),
                    "Chart.yaml": (
                        "name: consumer\ndependencies:\n"
                        "  - name: shared\n    version: 1.0.0\n"
                        "  - name: outside-chart\n    version: 2.0.0\n"
                    ),
                    "pom.xml": (
                        "<project><groupId>com.example</groupId><artifactId>app</artifactId>"
                        "<version>1</version><dependencies>"
                        "<dependency><groupId>com.example</groupId><artifactId>lib-a</artifactId>"
                        "<version>1</version></dependency>"
                        "<dependency><groupId>org.outside</groupId><artifactId>lib-z</artifactId>"
                        "<version>1</version></dependency>"
                        "</dependencies></project>\n"
                    ),
                    "package.json": json.dumps(
                        {
                            "name": "consumer",
                            "dependencies": {"@example/kit": "^1", "outside": "^2"},
                        }
                    ),
                    "c.events": "subscribe orders.created@v1\nsubscribe outside.topic\n",
                },
            }
        )
        self.declare()
        kinds = (*relationships.BUILTIN, *self.load())
        self.assertEqual(len(kinds), 5)
        layer = self.build(kinds=kinds)
        outside = {
            "terraform_module": "hashicorp/outside/aws",
            "helm_chart": "outside-chart",
            "maven_artifact": "org.outside:lib-z",
            "npm_package": "outside",
            KIND: "outside.topic",
        }
        unresolved = layer.dependencies["unresolved_targets_outside_estate"]
        for kind in kinds:
            with self.subTest(kind=kind.name):
                self.assertEqual(
                    unresolved[kind.name],
                    {outside[kind.name]: {"references": 1, "referenced_by": ["consumer"]}},
                )
                self.assertEqual(layer.kinds[kind.name]["edges"], 1)
                self.assertEqual(layer.kinds[kind.name]["unresolved"], 1)
