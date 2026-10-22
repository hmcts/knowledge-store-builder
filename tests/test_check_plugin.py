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
FROM = "git+https://github.com/hmcts/knowledge-store-mcp@"


def server(tag="v0.1.0", store_id="example-store", command="uvx", source=None):
    args = ["--from", source if source is not None else FROM + tag]
    args += ["knowledge-store-mcp"]
    if store_id is not None:
        args += ["--store-id", store_id]
    return {"command": command, "args": args}


def declaration(**kwargs):
    return {"mcpServers": {"knowledge-store": server(**kwargs)}}


class PluginGateTest(SettingsIsolated):
    def store(self, tmp, manifest_id="example-store"):
        root = Path(tmp)
        (root / "knowledge").mkdir()
        if manifest_id is not None:
            (root / "knowledge" / "store-manifest.json").write_text(
                json.dumps({"store": {"id": manifest_id}}), encoding="utf-8"
            )
        config.configure(root=str(root))
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
        self.assertIn("v0.1.0", out)
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
        bad = [
            "git+https://github.com/hmcts/knowledge-store-mcp@main",
            "git+https://github.com/hmcts/knowledge-store-mcp@latest",
            "git+https://github.com/hmcts/knowledge-store-mcp",
            "git+https://github.com/hmcts/knowledge-store-mcp@v0.1",
            "git+https://github.com/other/knowledge-store-mcp@v0.1.0",
        ]
        for source in bad:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                self.store(tmp)
                plugin = self.plugin(tmp, declaration(source=source))
                code, _out, err = self.run_stage("--plugin", str(plugin))
                self.assertRefused(code, err, "--from", source)

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
            plugin = self.plugin(tmp, declaration(tag="v0.10.0"))
            code, _out, err = self.run_stage("--plugin", str(plugin))
        self.assertEqual((code, err), (0, ""))

    def test_options_written_with_equals_are_read(self):
        """Catches `--store-id=x` reading as absent."""
        body = {
            "mcpServers": {
                "knowledge-store": {
                    "command": "uvx",
                    "args": [
                        "--from=" + FROM + "v0.1.0",
                        "knowledge-store-mcp",
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


if __name__ == "__main__":
    unittest.main()
