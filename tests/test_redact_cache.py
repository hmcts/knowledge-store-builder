"""Tests for `redact-cache`, the widened `check-evidence`, and the pasted-mention rule.

Every test runs the real stages over a real gzip cache written into a temporary
store. Nothing is stubbed: the rules, the reader, the deterministic writer and the
printed report are production code, and assertions land on the bytes written and
the text printed.

All names are invented, and the address is the reserved `xxxxx@xxxxx.example`.
The invented person is searched for in everything a stage prints: a report that
carries a matched value has published it again, to a log read more widely and kept
longer than the artefact.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import check_evidence  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io as pio  # noqa: E402
from knowledgestore import redact_cache  # noqa: E402
from knowledgestore import sensitive  # noqa: E402

NAME = "Jane Example"
MENTION = f"[@{NAME}|https://chat.example/profile/1]"
EMAIL = "xxxxx@xxxxx.example"
LATER_RULE = {"record-reference": r"\bREC/[0-9]{4}\b"}


def run_quietly(stage, argv=None):
    """(exit code, everything the stage printed to either stream)."""
    printed = StringIO()
    with redirect_stdout(printed), redirect_stderr(printed):
        code = stage.main(argv or [])
    return code, printed.getvalue()


class StoreWithACachedTracker(SettingsIsolated):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_root = config.ROOT
        config.configure(root=self._tmp.name)
        self.addCleanup(config.configure, root=str(self._old_root))
        self.cache_path = config.TICKET_TRACKER_PATH
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def cache(self, records: dict) -> None:
        pio.write_gzip_json(self.cache_path, dict(sorted(records.items())))

    def cached(self) -> dict:
        return pio.read_gzip_json_dict(self.cache_path)


class RedactCacheTest(StoreWithACachedTracker):
    """The break each test catches: a rule added after a fetch never reaching the
    cache, which is the artefact a store commits and publishes."""

    def record(self, text: str) -> dict:
        return {
            "summary": f"summary {text}",
            "description": f"description {text}",
            "resolution": f"resolution {text}",
            "comments": [f"first {text}", "a clean comment", f"last {text}"],
            "status": "Done",
        }

    def test_a_rule_added_after_the_fetch_is_applied_to_every_text_field(self):
        """Catches the cache not being re-redacted: a record fetched before the
        rule existed keeps the text in summary, description, resolution and every
        comment, and a resolved ticket is never fetched again to fix it."""
        self.cache({"AAA-1": self.record("see REC/1234 now"), "AAA-2": {"summary": "clean"}})
        config.SENSITIVE_PATTERNS = {**config.SENSITIVE_PATTERNS, **LATER_RULE}

        code, output = run_quietly(redact_cache)

        self.assertEqual(code, 0)
        stored = self.cached()
        self.assertEqual(stored["AAA-1"]["summary"], "summary see [record reference withheld] now")
        self.assertEqual(
            stored["AAA-1"]["description"], "description see [record reference withheld] now"
        )
        self.assertEqual(
            stored["AAA-1"]["resolution"], "resolution see [record reference withheld] now"
        )
        self.assertEqual(
            stored["AAA-1"]["comments"],
            [
                "first see [record reference withheld] now",
                "a clean comment",
                "last see [record reference withheld] now",
            ],
        )
        self.assertEqual(stored["AAA-1"]["status"], "Done")
        self.assertEqual(stored["AAA-2"], {"summary": "clean"})
        self.assertIn("redacted 5 spans in 1 of 2 cached tracker records", output)
        self.assertIn("record-reference 5", output)
        self.assertNotIn("REC/1234", output)

    def test_a_second_run_changes_nothing(self):
        """Catches a re-redaction that is not idempotent (a placeholder matching
        a rule, or a rewrite that churns bytes): a committed artefact would
        change on every run."""
        self.cache({"AAA-1": self.record(f"REC/1234 and {MENTION} and {EMAIL}")})
        config.SENSITIVE_PATTERNS = {**config.SENSITIVE_PATTERNS, **LATER_RULE}
        run_quietly(redact_cache)
        first = self.cache_path.read_bytes()

        _, output = run_quietly(redact_cache)

        self.assertEqual(self.cache_path.read_bytes(), first)
        self.assertIn("redacted 0 spans in 0 of 1", output)

    def test_a_default_pasted_mention_in_a_cached_comment_is_withheld(self):
        """Catches the shipped rule not reaching a cache written before it
        existed: the exact case of the report this stage answers."""
        self.cache({"AAA-1": {"summary": "s", "comments": [f"ping {MENTION} please"]}})

        _, output = run_quietly(redact_cache)

        self.assertEqual(
            self.cached()["AAA-1"]["comments"], ["ping [pasted mention withheld] please"]
        )
        self.assertNotIn(NAME, output)

    def test_no_cache_is_nothing_to_do_not_a_failure(self):
        code, output = run_quietly(redact_cache)
        self.assertEqual(code, 0)
        self.assertIn("nothing to redact", output)
        self.assertFalse(self.cache_path.exists())

    def test_type_status_and_parent_are_redacted_too(self):
        """Catches the fields `fetch-tickets` redacts being skipped on re-run."""
        self.cache(
            {"AAA-1": {"summary": "s", "type": MENTION, "status": MENTION, "parent": MENTION}}
        )
        run_quietly(redact_cache)
        stored = self.cached()["AAA-1"]
        for field in ("type", "status", "parent"):
            self.assertEqual(stored[field], "[pasted mention withheld]")

    def test_a_change_names_the_stages_that_rebuild_the_copies(self):
        """Catches a clean cache reported as a clean store: the page, index and
        intent layer still hold the old text until these stages re-run."""
        self.cache({"AAA-1": {"summary": MENTION}})
        _, output = run_quietly(redact_cache)
        order = [output.index(f"knowledgestore {stage}\n") for stage in redact_cache.REBUILD]
        self.assertEqual(order, sorted(order))
        for stage in ("intent", "answers-layer", "index", "explorer", "check-evidence"):
            self.assertIn(f"knowledgestore {stage}\n", output)
        _, again = run_quietly(redact_cache)
        self.assertNotIn("Re-run", again)

    def test_denied_and_absent_entries_survive_untouched(self):
        """Catches a walker assuming every cached entry is a record: the cache
        also holds `denied` and `absent` outcomes."""
        self.cache({"AAA-1": "denied", "AAA-2": None, "AAA-3": {"summary": MENTION}})
        run_quietly(redact_cache)
        stored = self.cached()
        self.assertEqual(stored["AAA-1"], "denied")
        self.assertEqual(stored["AAA-3"]["summary"], "[pasted mention withheld]")


class AtomicWriteTest(StoreWithACachedTracker):
    """Catches a writer that truncates the live file before it has the new
    content: an interrupt left a tracker cache whose resolved tickets are never
    fetched again, so nothing could rebuild it."""

    def test_an_error_mid_write_leaves_the_original_file_byte_identical(self):
        self.cache({"AAA-1": {"summary": "kept"}})
        before = self.cache_path.read_bytes()
        with self.assertRaises(RuntimeError):
            with pio.gzip_text(self.cache_path) as out:
                out.write('{"AAA-1": {"summary": "half')
                raise RuntimeError("disk full")
        self.assertEqual(self.cache_path.read_bytes(), before)
        self.assertEqual(
            sorted(p.name for p in self.cache_path.parent.iterdir()), [self.cache_path.name]
        )


class CheckEvidenceReadsTheTrackerCacheTest(StoreWithACachedTracker):
    """The break each test catches: a gate that reads only ticket descriptions
    and so passes a store whose far larger tracker cache holds a person's name."""

    def test_a_cached_comment_matching_a_rule_fails_the_default_gate(self):
        """Catches check-evidence ignoring the tracker cache."""
        self.cache({"AAA-9": {"summary": "s", "comments": ["fine", f"cc {MENTION}"]}})

        code, output = run_quietly(check_evidence)

        self.assertEqual(code, 1)
        self.assertIn("AAA-9", output)
        self.assertIn("field comments", output)
        self.assertIn("pasted-mention", output)

    def test_the_gate_never_prints_the_text_it_matched(self):
        """Catches a finding that carries the value: a build log would republish
        the name. Both streams are captured together."""
        self.cache(
            {
                "AAA-9": {
                    "summary": f"{NAME} {EMAIL}",
                    "description": MENTION,
                    "comments": [f"greetings {MENTION}", f"correspondence {EMAIL}"],
                }
            }
        )

        code, output = run_quietly(check_evidence)

        self.assertEqual(code, 1)
        for text in (NAME, EMAIL, "chat.example", "greetings", "correspondence"):
            self.assertNotIn(text, output)

    def test_a_redacted_cache_passes(self):
        self.cache({"AAA-9": {"summary": "s", "comments": [f"cc {MENTION}"]}})
        run_quietly(redact_cache)
        code, _ = run_quietly(check_evidence)
        self.assertEqual(code, 0)

    def test_a_named_tracker_artefact_is_read_too(self):
        """Catches the tracker fields being read only for the default path."""
        other = Path(self._tmp.name) / "other-tracker.json.gz"
        pio.write_gzip_json(other, {"AAA-1": {"summary": MENTION}})
        code, output = run_quietly(check_evidence, [str(other)])
        self.assertEqual(code, 1)
        self.assertNotIn(NAME, output)


class PastedMentionRuleTest(SettingsIsolated):
    """The break each test catches: the shipped rule missing a mention shape, or
    swallowing code that merely looks like one."""

    def withheld(self, text: str) -> str:
        return sensitive.redact(text)

    def test_a_profile_link_mention_is_withheld_whole(self):
        self.assertEqual(self.withheld(f"hi {MENTION}!"), "hi [pasted mention withheld]!")

    def test_a_name_containing_a_pipe_is_withheld_whole(self):
        self.assertEqual(
            self.withheld("[@Jane | Example Ltd|https://chat.example/profile/2]"),
            "[pasted mention withheld]",
        )

    def test_a_mention_linking_an_email_is_withheld_whole(self):
        """Catches the email rule leaving `[@Name|mailto:[email address withheld]]`:
        the name would stay beside its placeholder."""
        self.assertEqual(self.withheld(f"[@{NAME}|mailto:{EMAIL}]"), "[pasted mention withheld]")
        self.assertEqual(
            self.withheld(f"[@{NAME}|mailto:[email address withheld]]"),
            "[pasted mention withheld]",
        )

    def test_an_email_display_name_leaves_no_link_behind(self):
        """Catches the name class being unable to cross the email placeholder's
        `]`: the email rule runs first and `[@[email address withheld]|<link>]`
        kept the profile link, and check-evidence then matched nothing."""
        raw = f"see [@{EMAIL}|https://chat.example/p/1] ok"
        self.assertEqual(self.withheld(raw), "see [pasted mention withheld] ok")
        # The other order: the mention rule meeting the raw email, and meeting
        # the placeholder an earlier email pass left.
        rule = re.compile(config.DEFAULT_SENSITIVE_PATTERNS["pasted-mention"])
        self.assertEqual(rule.sub("X", raw), "see X ok")
        after_email = "[@[email address withheld]|https://chat.example/p/1]"
        self.assertEqual(rule.sub("X", after_email), "X")

    def test_attribute_fragments_are_left_alone(self):
        for code in ("[@attr=value]", "[@attr]", "x[@id='a'] y"):
            with self.subTest(code=code):
                self.assertEqual(self.withheld(code), code)

    def test_the_rule_is_the_shipped_default(self):
        self.assertIn("pasted-mention", config.DEFAULT_SENSITIVE_PATTERNS)
        self.assertTrue(
            re.compile(config.DEFAULT_SENSITIVE_PATTERNS["pasted-mention"]).search(MENTION)
        )


if __name__ == "__main__":
    unittest.main()
