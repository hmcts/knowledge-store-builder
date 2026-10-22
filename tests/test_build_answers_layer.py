"""`answers-layer` turns a directory of Markdown into the layer the server serves.

Every test builds a real store in a temporary directory, with a real git
repository and real commits at fixed dates, runs the stage's `main` and reads
the JSON file it wrote. Expected values are written out by hand: the dates are
the ones the fixture commits with, and the SHAs are asked of git, never of the
code under test. Each test names the break it catches.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated, store_root
from knowledgestore import build_answers_layer, config, store_manifest
from knowledgestore.git_env import git_env

# Not UTC: git spells UTC `Z` or `+00:00` depending on its version, and these
# tests are about which commit is read, not about that spelling.
FIRST = "2024-02-03T04:05:06+01:00"
SECOND = "2024-05-06T07:08:09+01:00"
# Every fixture commit is authored at this date and committed at another, so an
# entry dated by authorship rather than by commit is visible.
AUTHORED = "2020-01-01T00:00:00+00:00"

# How knowledge-store-mcp v0.1.1 reads an answers layer, transcribed from
# `tools_tickets._add_answers`, `_entry`, `_plain_segment` and `_plain_path` at
# that tag. The server skips an entry whose id fails `_plain_segment`, without
# saying so to the store, and drops a `source` that fails `_plain_path`.
SERVER_READS = ("title", "html", "source")


def _server_plain_segment(segment: str) -> bool:
    return bool(segment.strip(".")) and all(c.isalnum() or c in "._-" for c in segment)


def _server_plain_path(value) -> bool:
    return isinstance(value, str) and all(map(_server_plain_segment, value.split("/")))


def _git(root: Path, *arguments: str, date: str | None = None) -> str:
    env = git_env()
    if date is not None:
        env = {**env, "GIT_AUTHOR_DATE": AUTHORED, "GIT_COMMITTER_DATE": date}
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


class _Store(SettingsIsolated):
    def setUp(self):
        self.root = store_root(self)
        config.configure(root=str(self.root))
        _git(self.root, "init", "-q")
        self.answers = self.root / "docs" / "answers"
        self.out = self.root / "knowledge" / "answers.json"

    def write(self, rel: str, text: str) -> Path:
        path = self.answers / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit(self, date: str) -> str:
        _git(self.root, "add", "docs")
        _git(self.root, "commit", "-q", "-m", "answers", date=date)
        return _git(self.root, "rev-parse", "HEAD")

    def build(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = sys.argv
        sys.argv = [
            "knowledgestore answers-layer",
            "--markdown",
            str(self.answers),
            "--out",
            str(self.out),
        ]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = build_answers_layer.main()
        finally:
            sys.argv = argv
        return code, out.getvalue(), err.getvalue()

    def layer(self) -> dict:
        code, _, err = self.build()
        self.assertEqual(code, 0, err)
        return json.loads(self.out.read_text(encoding="utf-8"))


class OneEntryPerFile(_Store):
    def test_two_files_give_two_entries_with_their_headings_and_sources(self):
        """Breaks if a file is dropped, an id keeps its `.md` suffix, the title is
        not the first heading, or `source` is not the store-relative path."""
        self.write("apply.md", "Intro line.\n\n# Applying for a licence\n\n## Later\n")
        self.write("pay.md", "# Paying the fee\n\nBy card.\n")
        self.commit(FIRST)
        layer = self.layer()
        self.assertEqual(sorted(layer), ["apply", "pay"])
        self.assertEqual(layer["apply"]["title"], "Applying for a licence")
        self.assertEqual(layer["pay"]["title"], "Paying the fee")
        self.assertEqual(layer["apply"]["source"], "docs/answers/apply.md")
        self.assertEqual(layer["pay"]["source"], "docs/answers/pay.md")
        self.assertEqual(layer["pay"]["html"], "<h2>Paying the fee</h2><p>By card.</p>")

    def test_a_file_with_no_heading_takes_its_title_from_its_stem(self):
        """Breaks if a heading-less file is given an empty title, which the server
        would serve as an answer with nothing to name it."""
        self.write("refunds.md", "Refunds take five days.\n")
        self.commit(FIRST)
        self.assertEqual(self.layer()["refunds"]["title"], "refunds")

    def test_an_id_the_server_would_skip_is_refused_and_nothing_is_written(self):
        """Breaks if a file in a subdirectory, whose id carries a `/`, is written:
        the server skips such an entry without telling the store, so the answer
        would be published and never served."""
        self.write("apply.md", "# Apply\n")
        self.write("journeys/pay.md", "# Pay\n")
        self.commit(FIRST)
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("journeys/pay", err)
        self.assertFalse(self.out.exists())

    def test_a_directory_outside_the_store_is_refused(self):
        """Breaks if a file outside the store root is written with a `source` no
        clone of the store can open, or ends the stage in a traceback."""
        outside = store_root(self) / "answers"
        outside.mkdir()
        (outside / "apply.md").write_text("---\ndate: 2023-01-15\n---\n# Apply\n")
        self.answers = outside
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("outside the store root", err)
        self.assertFalse(self.out.exists())

    def test_a_directory_with_no_markdown_is_refused(self):
        """Breaks if an empty or mistyped `--from` writes an empty layer, which the
        manifest would record as present and the server would serve as nothing."""
        self.answers.mkdir(parents=True)
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("no .md files", err)
        self.assertFalse(self.out.exists())


class WhenEachAnswerWasWritten(_Store):
    def test_a_front_matter_date_wins_over_the_commit_date(self):
        """Breaks if the commit date replaces the date the author wrote down, or if
        the front matter leaks into the served html."""
        self.write("apply.md", "---\ntitle: ignored\ndate: 2023-01-15\n---\n# Apply\n\nText.\n")
        sha = self.commit(FIRST)
        entry = self.layer()["apply"]
        self.assertEqual(entry["as_of"], "2023-01-15")
        self.assertEqual(entry["commit"], sha)
        self.assertEqual(entry["html"], "<h2>Apply</h2><p>Text.</p>")

    def test_without_front_matter_the_last_commit_date_and_sha_are_used(self):
        """Breaks if the first commit is read instead of the last, or the author
        date or the working-tree time stands in for the commit date."""
        self.write("apply.md", "# Apply\n")
        self.commit(FIRST)
        self.write("apply.md", "# Apply\n\nRevised.\n")
        sha = self.commit(SECOND)
        entry = self.layer()["apply"]
        self.assertEqual(entry["as_of"], SECOND)
        self.assertEqual(entry["commit"], sha)

    def test_an_uncommitted_file_with_a_front_matter_date_has_no_commit(self):
        """Breaks if an uncommitted file is attributed to a commit that never held
        it."""
        self.write("apply.md", "# Apply\n")
        self.commit(FIRST)
        self.write("draft.md", "---\ndate: '2023-03-04'\n---\n# Draft\n")
        entry = self.layer()["draft"]
        self.assertEqual(entry["as_of"], "2023-03-04")
        self.assertIsNone(entry["commit"])

    def test_a_file_with_neither_a_date_nor_a_commit_is_refused(self):
        """Breaks if an answer with no date is written: served, it would read as
        today's knowledge."""
        self.write("draft.md", "# Draft\n")
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("draft.md", err)
        self.assertFalse(self.out.exists())

    def test_a_utc_commit_date_is_spelled_one_way_whatever_git_wrote(self):
        """Breaks if git's `Z` spelling of UTC is passed through: newer git writes
        `Z` and older git `+00:00`, so the same commit would give different bytes
        on two machines. Stubbed at the git boundary, because which spelling the
        installed git writes is the thing being taken out of the answer."""

        def newer_git(arguments):
            return "abc123\x1f2024-02-03T04:05:06Z\n"

        found = build_answers_layer.last_commit(self.answers / "a.md", run=newer_git)
        self.assertEqual(found, ("2024-02-03T04:05:06+00:00", "abc123"))


class Determinism(_Store):
    def test_two_runs_at_one_commit_write_identical_bytes(self):
        """Breaks if file order, key order or anything else varies between runs."""
        for name in ("b", "a", "c"):
            self.write(f"{name}.md", f"# {name}\n")
        self.commit(FIRST)
        self.build()
        first = self.out.read_bytes()
        self.out.unlink()
        self.build()
        self.assertEqual(self.out.read_bytes(), first)
        self.assertEqual(list(json.loads(first)), ["a", "b", "c"])


class TheServerReadsIt(_Store):
    def test_every_entry_has_the_shape_the_server_reads(self):
        """Breaks if a field the server reads at v0.1.1 is renamed or retyped, if an
        id or source would fail the server's own path rules, or if a field the
        server does not know is added beyond `as_of` and `commit`."""
        self.write("apply.md", "# Apply\n")
        self.write("no-heading.md", "Text.\n")
        self.commit(FIRST)
        layer = self.layer()
        self.assertTrue(layer)
        for entry_id, entry in layer.items():
            self.assertTrue(_server_plain_segment(entry_id), entry_id)
            self.assertEqual(set(entry), {*SERVER_READS, "as_of", "commit"})
            for key in SERVER_READS:
                self.assertIsInstance(entry[key], str, key)
            self.assertTrue(_server_plain_path(entry["source"]), entry["source"])

    def test_the_output_passes_the_manifest_declaration_as_an_answers_layer(self):
        """Breaks if the generated file cannot be declared as an answers layer: the
        manifest must record it present, as a file, with kind `answers`, which is
        what the server's `nameable_layers` requires before it reads one."""
        self.write("apply.md", "# Apply\n")
        self.commit(FIRST)
        self.layer()
        declaration = self.root / "config" / "store-extra-layers.json"
        declaration.parent.mkdir(parents=True)
        declaration.write_text(
            json.dumps(
                {
                    "faq": {
                        "path": "knowledge/answers.json",
                        "description": "Written answers.",
                        "kind": "answers",
                    }
                }
            ),
            encoding="utf-8",
        )
        faq = store_manifest.extra_layers(self.root)["faq"]
        self.assertEqual(faq["path"], "knowledge/answers.json")
        self.assertEqual(faq["kind"], "answers")
        self.assertIs(faq["present"], True)


if __name__ == "__main__":
    unittest.main()
