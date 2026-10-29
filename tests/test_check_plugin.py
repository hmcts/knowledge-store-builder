"""The plugin gate: a store plugin must launch the server for this store.

The break this catches: a plugin naming the wrong `--store-id`, or a floating
ref, fails for every asker when their assistant starts, with a message about a
missing clone. Every test builds a real store directory and drives the stage.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import check_plugin as gate  # noqa: E402
from knowledgestore import cli, config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GOOD_LAYERS = {
    "index": {"present": True},
    "relationships": {"present": True, "recognised": True},
}
# Written out, not built from the module's constants: the form a store copies is the
# claim, and a test assembling it from `check_plugin` would agree with whatever it said.
FROM = (
    "hmcts-knowledge-store-builder[server] @ git+https://github.com/hmcts/knowledge-store-builder@"
)
ARCHIVED = "git+https://github.com/hmcts/knowledge-store-mcp@"


def server(
    tag=gate.DOCUMENTED_TAG,
    store_id="example-store",
    command="uvx",
    source=None,
    entry=("knowledgestore", "serve"),
):
    args = ["--from", source if source is not None else FROM + tag]
    args += list(entry)
    if store_id is not None:
        args += ["--store-id", store_id]
    return {"command": command, "args": args}


# Written out, as FROM is: the feed URL a store copies is the claim.
FEED = "https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/"
FEED_FROM = "hmcts-knowledge-store-builder[server]=="


def feed_server(version="0.22.0", store_id="example-store", before=None, source=None):
    """The documented launch: the feed named by --index, an exact == version."""
    args = ["--index", FEED] if before is None else list(before)
    args += ["--from", source if source is not None else FEED_FROM + version]
    args += ["knowledgestore", "serve", "--store-id", store_id]
    return {"command": "uvx", "args": args}


def declaration(**kwargs):
    return {"mcpServers": {"knowledge-store": server(**kwargs)}}


class PluginGateTest(SettingsIsolated):
    def store(self, tmp, manifest_id="example-store", layers=GOOD_LAYERS):
        root = Path(tmp)
        (root / "knowledge").mkdir()
        if manifest_id is not None:
            (root / "knowledge" / "store-manifest.json").write_text(
                json.dumps({"store": {"id": manifest_id}, "layers": layers}), encoding="utf-8"
            )
        config.configure(root=str(root), STORE_ID=manifest_id or "")
        return root

    def plugin(self, tmp, body, form=".mcp.json", name="plugin"):
        directory = Path(tmp) / name
        (directory / ".claude-plugin").mkdir(parents=True)
        path = directory / (".mcp.json" if form == ".mcp.json" else ".claude-plugin/plugin.json")
        path.write_text(json.dumps(body), encoding="utf-8")
        return directory

    def run_stage(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gate.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def assertRefused(self, code, err, *needles):
        self.assertEqual(code, 1, err)
        for needle in needles:
            self.assertIn(needle, err)

    def test_the_documented_tag_is_the_one_in_docs_mcp_server_txt(self):
        """Catches a bump of docs/mcp-server.txt that the constant did not follow."""
        first = (ROOT / "docs/mcp-server.txt").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first, f"tag {gate.DOCUMENTED_TAG}")

    def test_the_stage_is_registered_and_owns_its_arguments(self):
        """Catches a stage missing from the CLI, or one that refuses --plugin."""
        self.assertEqual(cli.STAGES["check-plugin"][0], "check_plugin")
        self.assertFalse(cli.refuses_arguments("check-plugin", ["--plugin", "x"]))

    def test_an_mcp_json_declaration_that_agrees_passes_and_names_tag_and_id(self):
        """Catches a stage that cannot read the .mcp.json form, or a success line
        that does not say what it verified."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            plugin = self.plugin(tmp, declaration())
            code, out, err = self.run_stage("--plugin", str(plugin))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn(gate.DOCUMENTED_TAG, out)
        self.assertIn("example-store", out)

    def test_a_plugin_json_declaration_at_the_repository_root_passes_by_default(self):
        """Catches the plugin.json form being unread, and --plugin not defaulting
        to the store root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.store(tmp)
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "x", **declaration()}), encoding="utf-8"
            )
            code, out, err = self.run_stage()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("example-store", out)

    def test_no_knowledge_store_server_fails(self):
        """Catches a declaration naming only some other server passing."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            plugin = self.plugin(tmp, {"mcpServers": {"other": server()}})
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertRefused(code, err, "no `knowledge-store` server")

    def test_no_declaration_at_all_fails(self):
        """Catches an empty plugin directory reading as agreement."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            empty = Path(tmp) / "empty"
            empty.mkdir()
            code, _out, err = self.run_stage("--plugin", str(empty))
        self.assertRefused(code, err, "no .mcp.json")

    def test_a_command_other_than_uvx_fails(self):
        """Catches the command not being checked."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            plugin = self.plugin(tmp, declaration(command="npx"))
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertRefused(code, err, "'npx'", "uvx")

    def test_a_floating_or_missing_ref_fails(self):
        """Catches a branch, latest, no ref, another repository or a partial tag
        being accepted as an exact tag."""
        base = FROM.rsplit("@", 1)[0]
        bad = [
            base + "@main",
            base + "@latest",
            base,
            base + "@v0.22",
            FROM.replace("hmcts/", "other/") + "v0.22.0",
            "git+https://github.com/hmcts/knowledge-store-builder@v0.22.0",  # no [server]
            "git+https://github.com/hmcts/knowledge-store-mcp@main",
            "git+https://github.com/hmcts/knowledge-store-mcp@v0.1",
        ]
        for source in bad:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                self.store(tmp)
                plugin = self.plugin(tmp, declaration(source=source))
                code, _out, err = self.run_stage("--plugin", str(plugin))
                self.assertRefused(code, err, "--from", source)

    def test_a_launch_that_does_not_run_serve_fails(self):
        """Catches a correct pin launching the wrong entry point: uvx would run
        some other `knowledgestore` stage, or the old executable, which the
        library does not install."""
        for entry in [("knowledge-store-mcp",), ("knowledgestore",), ("knowledgestore", "status")]:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                self.store(tmp)
                plugin = self.plugin(tmp, declaration(entry=entry))
                code, _out, err = self.run_stage("--plugin", str(plugin))
                self.assertRefused(code, err, "knowledgestore serve")

    def test_the_archived_server_form_warns_naming_the_new_form_and_passes(self):
        """Catches a store still pinned to the archived repository being failed
        (its pin keeps working), or passing in silence (nothing tells it to move)."""
        archived = declaration(source=ARCHIVED + "v0.3.0", entry=("knowledge-store-mcp",))
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            plugin = self.plugin(tmp, archived)
            code, out, err = self.run_stage("--plugin", str(plugin))
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("warning:"), 1, err)
        self.assertIn("knowledge-store-mcp", err)
        self.assertIn(FROM + gate.DOCUMENTED_TAG, err)
        self.assertIn("knowledgestore serve", err)
        self.assertIn("v0.3.0", out)
        self.assertIn("example-store", out)

    def test_the_archived_form_is_still_held_to_an_exact_tag_and_the_store_id(self):
        """Catches the warning path becoming a way round the pin and id checks."""
        floating = declaration(source=ARCHIVED + "main", entry=("knowledge-store-mcp",))
        wrong_id = declaration(
            source=ARCHIVED + "v0.3.0", entry=("knowledge-store-mcp",), store_id="other"
        )
        for body, needle in [(floating, "--from"), (wrong_id, "'other'")]:
            with self.subTest(needle=needle), tempfile.TemporaryDirectory() as tmp:
                self.store(tmp)
                plugin = self.plugin(tmp, body)
                code, _out, err = self.run_stage("--plugin", str(plugin))
                self.assertRefused(code, err, needle)

    def feed(self, body):
        """(code, out, err) for a plugin declaring `body` as its server."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            plugin = self.plugin(tmp, {"mcpServers": {"knowledge-store": body}})
            return self.run_stage("--plugin", str(plugin))

    def test_the_feed_url_constant_is_the_hmcts_lib_feed(self):
        """Catches the constant drifting from the feed the docs and askers use."""
        self.assertEqual(gate.FEED_INDEX, FEED)

    def test_the_feed_form_passes_and_names_version_and_id(self):
        """Catches the documented form being refused, or a success line that does
        not say which release and store it verified."""
        code, out, err = self.feed(feed_server())
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out.splitlines()[0].split(": ", 1)[1],
            ("launches knowledge-store v0.22.0 for store example-store"),
        )

    def test_the_feed_form_accepts_default_index_and_equals_spelling(self):
        """Catches the index being read only in its `--index <url>` spelling."""
        for before in (["--default-index", FEED], ["--index=" + FEED]):
            with self.subTest(before=before):
                code, _out, err = self.feed(feed_server(before=before))
                self.assertEqual((code, err), (0, ""))

    def test_the_feed_form_without_an_index_fails_naming_pypi(self):
        """Catches the dependency-confusion hole: the name is not on PyPI, so a
        launch with no --index resolves it there, where anyone can register it."""
        code, _out, err = self.feed(feed_server(before=[]))
        self.assertRefused(code, err, "PyPI", "--index")

    def test_the_feed_form_with_an_index_not_naming_the_feed_fails(self):
        """Catches an --index on another host passing as if it were the feed,
        and the legacy options putting PyPI or another index in play."""
        other = "https://pkgs.example.com/simple/"
        pypi = "https://pypi.org/simple/"
        for before in (
            ["--index", other],
            ["--default-index", pypi],
            ["--index", FEED, "--extra-index-url", pypi],
            ["--index", FEED, "--index-url", pypi],
            ["--extra-index-url", FEED, "--index-url", pypi],
            ["--index", FEED[:-1]],
        ):
            with self.subTest(before=before):
                code, _out, err = self.feed(feed_server(before=before))
                self.assertRefused(code, err, "--")
                self.assertNotIn("launches", _out)

    def test_a_foreign_host_is_named_in_the_refusal(self):
        """Catches a refusal that says nothing about which index was refused."""
        code, _out, err = self.feed(feed_server(before=["--index", "https://pkgs.example.com/s/"]))
        self.assertRefused(code, err, "https://pkgs.example.com/s/")

    def test_an_unsafe_index_strategy_fails(self):
        """Catches unsafe-best-match and unsafe-first-match passing: they pick the
        highest version across indexes, so a PyPI squat would win."""
        for strategy in ("unsafe-best-match", "unsafe-first-match"):
            with self.subTest(strategy=strategy):
                before = ["--index", FEED, "--index-strategy", strategy]
                code, _out, err = self.feed(feed_server(before=before))
                self.assertRefused(code, err, strategy)
        code, _out, err = self.feed(
            feed_server(before=["--index", FEED, "--index-strategy=unsafe-best-match"])
        )
        self.assertRefused(code, err, "unsafe-best-match")

    def test_the_first_index_strategy_passes(self):
        """Catches every --index-strategy being refused, not only the unsafe ones."""
        before = ["--index", FEED, "--index-strategy", "first-index"]
        code, _out, err = self.feed(feed_server(before=before))
        self.assertEqual((code, err), (0, ""))

    def test_an_index_after_the_program_does_not_count(self):
        """Catches an --index handed to the server, after `knowledgestore`, being
        read as uvx's: uvx would still resolve the name from PyPI."""
        body = feed_server(before=[])
        body["args"] += ["--index", FEED]
        code, _out, err = self.feed(body)
        self.assertRefused(code, err, "PyPI")

    def test_a_range_or_unpinned_feed_requirement_fails(self):
        """Catches a version range, a bare name or a partial version passing as an
        exact pin."""
        for source in (
            "hmcts-knowledge-store-builder[server]",
            "hmcts-knowledge-store-builder[server]>=0.22.0",
            "hmcts-knowledge-store-builder[server]==0.22",
            "hmcts-knowledge-store-builder[server]==0.22.*",
            "hmcts-knowledge-store-builder==0.22.0",
            "hmcts-knowledge-store-builder[server]~=0.22.0",
        ):
            with self.subTest(source=source):
                code, _out, err = self.feed(feed_server(source=source))
                self.assertRefused(code, err, "--from", source)

    def test_an_older_feed_version_warns_but_passes_and_a_newer_does_not_warn(self):
        """Catches the feed version not being compared with the documented tag the
        way a git tag is."""
        code, out, err = self.feed(feed_server(version="0.21.9"))
        self.assertEqual(code, 0, err)
        self.assertIn("warning: pinned v0.21.9 is older than", err)
        self.assertIn("v0.21.9", out)
        code, _out, err = self.feed(feed_server(version="99.0.0"))
        self.assertEqual((code, err), (0, ""))

    def test_the_feed_form_is_still_held_to_serve_and_the_store_id(self):
        """Catches the feed path being a way round the entry-point and id checks."""
        no_serve = feed_server()
        no_serve["args"] = no_serve["args"][:-3] + ["knowledgestore", "status"]
        code, _out, err = self.feed(no_serve)
        self.assertRefused(code, err, "knowledgestore serve")
        code, _out, err = self.feed(feed_server(store_id="other"))
        self.assertRefused(code, err, "'other'")
        gone = feed_server()
        gone["args"] = gone["args"][:-2]
        code, _out, err = self.feed(gone)
        self.assertRefused(code, err, "no --store-id")

    def test_no_from_fails(self):
        """Catches an unpinned server (no --from) passing."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            body = {
                "mcpServers": {
                    "knowledge-store": {"command": "uvx", "args": ["--store-id", "example-store"]}
                }
            }
            plugin = self.plugin(tmp, body)
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertRefused(code, err, "no --from")

    def test_a_missing_store_id_fails(self):
        """Catches a launch with no --store-id passing."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            plugin = self.plugin(tmp, declaration(store_id=None))
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertRefused(code, err, "no --store-id")

    def test_a_store_id_that_differs_from_the_manifest_fails_naming_both(self):
        """Catches the id not being compared with the manifest, or a message that
        omits either value."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, manifest_id="the-real-id")
            plugin = self.plugin(tmp, declaration(store_id="the-plugin-name"))
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertRefused(code, err, "'the-plugin-name'", "'the-real-id'")

    def test_a_missing_manifest_fails_rather_than_passing_unchecked(self):
        """Catches an absent manifest skipping the id comparison."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, manifest_id=None)
            plugin = self.plugin(tmp, declaration())
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertRefused(code, err, "store-manifest.json")

    def test_a_root_mcp_json_at_the_repository_root_fails_even_when_correct(self):
        """Catches the root .mcp.json being accepted: it also loads as project
        configuration for anyone working in the repository."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.store(tmp)
            (root / ".mcp.json").write_text(json.dumps(declaration()), encoding="utf-8")
            code, _out, err = self.run_stage()
            explicit_code, _o, explicit_err = self.run_stage("--plugin", str(root))
        self.assertRefused(code, err, "repository root", ".mcp.json")
        self.assertRefused(explicit_code, explicit_err, "repository root")

    def test_a_subdirectory_plugin_may_carry_an_mcp_json_beside_a_root_without_one(self):
        """Catches the root rule firing for a plugin that is not the repository root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.store(tmp)
            plugin = self.plugin(root, declaration(), name="plugins/store")
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertEqual((code, err), (0, ""))

    def test_an_older_tag_warns_but_passes(self):
        """Catches an older pin failing the stage, or passing in silence."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            plugin = self.plugin(tmp, declaration(tag="v0.0.9"))
            code, out, err = self.run_stage("--plugin", str(plugin))
        self.assertEqual(code, 0, err)
        self.assertIn("warning", err)
        self.assertIn("v0.0.9", err)
        self.assertIn("v0.0.9", out)

    def test_a_newer_tag_than_documented_does_not_warn(self):
        """Catches the comparison pointing the wrong way, warning on a store that
        is ahead of this library."""
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            plugin = self.plugin(tmp, declaration(tag="v0.99.0"))
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertEqual((code, err), (0, ""))

    def test_options_written_with_equals_are_read(self):
        """Catches `--store-id=x` reading as absent."""
        body = {
            "mcpServers": {
                "knowledge-store": {
                    "command": "uvx",
                    "args": [
                        "--from=" + FROM + gate.DOCUMENTED_TAG,
                        "knowledgestore",
                        "serve",
                        "--store-id=example-store",
                    ],
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp)
            plugin = self.plugin(tmp, body)
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertEqual((code, err), (0, ""))

    def run_layers(self, layers):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, layers=layers)
            plugin = self.plugin(tmp, declaration())
            return self.run_stage("--plugin", str(plugin))

    def test_a_missing_or_absent_layer_fails_naming_it(self):
        """Catches a plugin passing over a store whose server would answer from a
        fallback or not at all, and a message that does not name the layer."""
        cases = {
            "index": {**GOOD_LAYERS, "index": {"present": False}},
            "relationships": {**GOOD_LAYERS, "relationships": {"present": False}},
        }
        for name, layers in cases.items():
            with self.subTest(layer=name):
                code, _out, err = self.run_layers(layers)
                self.assertRefused(code, err, f"{name} layer")
        code, _out, err = self.run_layers({})
        self.assertRefused(code, err, "index layer", "relationships layer")

    def test_a_relationships_layer_that_is_not_recognised_fails(self):
        """Catches `present` alone being read as readable."""
        layers = {**GOOD_LAYERS, "relationships": {"present": True, "recognised": False}}
        code, _out, err = self.run_layers(layers)
        self.assertRefused(code, err, "relationships layer", "not recognised")

    def test_a_layer_not_recorded_warns_and_never_passes_silently(self):
        """Catches present: null being treated as present, or as a failure."""
        layers = {**GOOD_LAYERS, "index": {"present": None}}
        code, _out, err = self.run_layers(layers)
        self.assertEqual(code, 0, err)
        self.assertIn("warning", err)
        self.assertIn("index layer", err)

    def run_with_build_id(self, build_id, manifest_id="example-store"):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, manifest_id=manifest_id)
            plugin = self.plugin(tmp, declaration(store_id=manifest_id))
            config.configure(STORE_ID=build_id)
            return self.run_stage("--plugin", str(plugin))

    def test_a_build_id_that_differs_from_the_manifest_fails_naming_both(self):
        """Catches the next `manifest` run writing an id the plugin does not ask for."""
        code, _out, err = self.run_with_build_id("another-id")
        self.assertRefused(code, err, "KSB_STORE_ID", "'another-id'", "'example-store'")

    def test_a_build_id_that_agrees_passes(self):
        """Catches the build id check failing a store whose id agrees."""
        code, _out, err = self.run_with_build_id("example-store")
        self.assertEqual((code, err), (0, ""))

    def test_an_unresolved_build_id_is_said_and_still_passes(self):
        """Catches the build-id check skipping in silence, so that a CI step which
        loads no settings reads as covered by a check that never ran."""
        for value in ("", "  "):
            with self.subTest(value=value):
                code, _out, err = self.run_with_build_id(value)
                self.assertEqual(code, 0, err)
                self.assertIn(
                    "warning: build id not resolved (KSB_STORE_ID unset); its check was skipped",
                    err,
                )

    def test_a_resolved_build_id_does_not_print_the_skip_line(self):
        """Catches the skip line printed whether or not the id resolved."""
        code, _out, err = self.run_with_build_id("example-store")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("not resolved", err)


if __name__ == "__main__":
    unittest.main()
