"""Tests for knowledgestore/build_intent_index.py - intent index + ticket descriptions."""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_intent_index as intent  # noqa: E402
from knowledgestore import config  # noqa: E402


def make_descriptions():
    # The stage owns the shape, so a field added there is present here too.
    return intent.ticket_pool()


def make_files():
    return defaultdict(lambda: {"tickets": defaultdict(int), "first": None, "last": None})


HUMAN = {"name": "A Person", "email": "a.person@example.example"}


def make_commit(
    subject,
    body="",
    date="2024-05-01T10:00:00+00:00",
    repo="repo-a",
    path="src/x.ts",
    author=None,
    committer=None,
):
    return {
        "repository": repo,
        "subject": subject,
        "is_merge": False,
        "author_date": date,
        "body": body,
        "author": author or HUMAN,
        "committer": committer or author or HUMAN,
        "files": [{"path": path}],
    }


def write_ndjson(directory, commits):
    ndjson = Path(directory) / "commits.ndjson"
    ndjson.write_text("\n".join(json.dumps(c) for c in commits) + "\n", encoding="utf-8")
    return ndjson


class CleanDescriptionTest(SettingsIsolated):
    def test_strips_single_ticket_prefix(self):
        self.assertEqual(
            intent.clean_description("DD-24302: address field length changed to 35"),
            "address field length changed to 35",
        )

    def test_strips_multiple_ticket_prefixes_and_brackets(self):
        self.assertEqual(
            intent.clean_description("[DD-1] CCT-2 - do the thing"),
            "do the thing",
        )

    def test_leaves_ticketless_subjects_alone(self):
        self.assertEqual(intent.clean_description("plain subject."), "plain subject")


class JunkFilterTest(SettingsIsolated):
    def test_junk_descriptions_match(self):
        for junk in ("wip", "Fixed", "addressed PR comments", "update", "refactoring"):
            self.assertIsNotNone(intent.JUNK_DESCRIPTION.match(junk), junk)

    def test_real_descriptions_do_not_match(self):
        self.assertIsNone(
            intent.JUNK_DESCRIPTION.match("Increase Validation on Address Entry Fields")
        )


class CleanBodyTest(SettingsIsolated):
    """The body filter. Each case is a body kind measured in a real estate; the
    break each test catches is that kind leaking into the committed artefacts as
    a ticket link or a description."""

    def test_trailer_only_body_yields_nothing(self):
        body = (
            "Co-authored-by: A Person <a@example.example>\n"
            "Signed-off-by: B Person <b@example.example>\n"
            "\n"
            "---\n"
            "Change-Id: I0123456789abcdef0123456789abcdef01234567\n"
        )
        self.assertEqual(intent.clean_body(body), "")

    def test_dependency_bot_body_is_rejected(self):
        bumps = (
            "Bumps [some-lib](https://example.example/some-lib) from 4.17.20 to 4.17.21.\n"
            "- [Release notes](https://example.example/some-lib/releases)\n"
        )
        self.assertEqual(intent.clean_body(bumps), "")
        metadata = (
            "updated-dependencies:\n"
            "- dependency-name: some-lib\n"
            "  dependency-type: direct:production\n"
        )
        self.assertEqual(intent.clean_body(metadata), "")
        named = "Dependabot will resolve any conflicts as long as you do not alter this.\n"
        self.assertEqual(intent.clean_body(named), "")

    def test_merge_commit_list_body_is_rejected(self):
        body = (
            "* Add the address validation rules to the form\n"
            "* Correct the postcode lookup timeout\n"
            "* Tidy the shared pipe\n"
        )
        self.assertEqual(intent.clean_body(body), "")

    def test_body_embedding_original_authors_is_rejected(self):
        body = (
            "Author: A Person <a@example.example>\n"
            "Squashed the address validation work onto the release branch.\n"
        )
        self.assertEqual(intent.clean_body(body), "")

    def test_body_shorter_than_twenty_five_characters_is_rejected(self):
        self.assertEqual(intent.clean_body("Small tweak."), "")

    def test_real_prose_survives_with_separators_and_trailers_removed(self):
        body = (
            "Address entry now rejects lines longer than 35 characters,\n"
            "matching the downstream schema.\n"
            "\n"
            "-----\n"
            "Co-authored-by: A Person <a@example.example>\n"
        )
        self.assertEqual(
            intent.clean_body(body),
            "Address entry now rejects lines longer than 35 characters,\n"
            "matching the downstream schema.",
        )


PROSE_LINE = "Address entry now rejects long lines, matching the downstream schema."


class TrailerShapeTest(SettingsIsolated):
    """Git trailers are recognised by shape, not only by name. A fixed list of
    names misses the trailers a team invents, and the learned-boilerplate filter
    cannot rescue the miss: a trailer whose value is a unique hash never repeats,
    so it never crosses the repetition thresholds. Measured on one estate, bodies
    that were nothing but trailer lines were the majority of the bodies surviving
    cleaning, most of them one migration trailer carrying a commit hash.

    The break the dropping tests catch is machine metadata stored as intent
    evidence. The break the keeping tests catch is worse: a shape rule wide enough
    to eat `KEY: what changed`, which is the most valuable line a body can hold."""

    def test_an_unlisted_hyphenated_trailer_is_dropped(self):
        body = f"Former-commit-id: 0f1e2d3c4b5a69788796a5b4c3d2e1f009182736\n{PROSE_LINE}"
        self.assertEqual(intent.clean_body(body), PROSE_LINE)

    def test_a_body_of_nothing_but_trailers_is_rejected_entirely(self):
        body = (
            "Former-commit-id: 0f1e2d3c4b5a69788796a5b4c3d2e1f009182736\n"
            "Reviewed-on: https://example.example/c/12345\n"
            "Depends-on: I0123456789abcdef0123456789abcdef01234567\n"
        )
        self.assertEqual(intent.clean_body(body), "")

    def test_a_single_token_value_is_a_trailer_whatever_its_key(self):
        body = f"Severity: minor\nVerified: yes\n{PROSE_LINE}"
        self.assertEqual(intent.clean_body(body), PROSE_LINE)

    def test_a_bare_url_line_is_dropped(self):
        self.assertEqual(
            intent.clean_body(f"https://example.example/path\n{PROSE_LINE}"), PROSE_LINE
        )
        self.assertEqual(intent.clean_body("https://example.example/a/rather/long/path/here"), "")

    def test_a_ticket_reference_and_what_changed_is_kept(self):
        # The regression that matters most. `DD-8855: fixed the defendant lookup`
        # is trailer-shaped - hyphenated key, colon, value - and is the single most
        # valuable line a body can carry: the ticket and what changed, in the
        # author's own words. A shape rule wide enough to drop it would discard
        # exactly the evidence this stage exists to collect, and the loss would be
        # invisible in the artefact.
        body = "DD-8855: fixed the defendant lookup and cleared its cache"
        self.assertEqual(intent.clean_body(body), body)

    def test_a_reference_key_with_a_single_token_value_is_kept(self):
        # `S1192: SESSION_START_TIME` is a rule id and a symbol, not a trailer,
        # even though the value is one token.
        body = f"S1192: SESSION_START_TIME\n{PROSE_LINE}"
        self.assertEqual(intent.clean_body(body), body.rstrip("\n"))

    def test_a_ticket_key_with_a_single_token_value_is_kept(self):
        body = f"DD-8855: hotfix\n{PROSE_LINE}"
        self.assertEqual(intent.clean_body(body), body.rstrip("\n"))

    def test_a_plain_key_with_a_multi_word_value_is_kept(self):
        body = "Tests: added unit tests for the address pipe, covering the length rule"
        self.assertEqual(intent.clean_body(body), body)


class BodyTicketIdsTest(SettingsIsolated):
    def test_ids_are_first_appearance_order_and_capped_at_three(self):
        body = "Rolls up ZZ-9, AA-1, MM-5, BB-2 and CC-3 into the June release."
        self.assertEqual(intent.body_ticket_ids(body), ["ZZ-9", "AA-1", "MM-5"])

    def test_rejected_body_contributes_no_ids(self):
        self.assertEqual(intent.body_ticket_ids("* DD-1 one\n* DD-2 two\n* DD-3 three"), [])


class ApplyCommitTest(SettingsIsolated):
    def _commit(self, subject, merge=False, date="2024-05-01T10:00:00+00:00", body=""):
        return {
            "repository": "repo-a",
            "subject": subject,
            "is_merge": merge,
            "author_date": date,
            "body": body,
            "files": [{"path": "src/x.ts"}],
        }

    def test_merge_and_ticketless_commits_are_skipped(self):
        files, descriptions = make_files(), make_descriptions()
        self.assertFalse(
            intent.apply_commit(self._commit("DD-1: x", merge=True), files, descriptions)
        )
        self.assertFalse(intent.apply_commit(self._commit("no ticket here"), files, descriptions))
        self.assertEqual(len(files), 0)

    def test_ticketed_commit_updates_files_and_descriptions(self):
        files, descriptions = make_files(), make_descriptions()
        self.assertTrue(
            intent.apply_commit(
                self._commit("DD-9: introduce address validation rules"), files, descriptions
            )
        )
        self.assertEqual(files["src/x.ts"]["tickets"]["DD-9"], 1)
        self.assertEqual(files["src/x.ts"]["first"], "2024-05-01")
        info = descriptions["DD-9"]
        self.assertEqual(info["count"], 1)
        self.assertIn("introduce address validation rules", info["descriptions"])

    def test_junk_description_counts_commit_but_keeps_no_text(self):
        files, descriptions = make_files(), make_descriptions()
        intent.apply_commit(self._commit("DD-9: wip"), files, descriptions)
        self.assertEqual(descriptions["DD-9"]["count"], 1)
        self.assertEqual(len(descriptions["DD-9"]["descriptions"]), 0)

    def test_body_ticket_is_indexed_when_the_subject_names_none(self):
        """Catches the loss of every file -> ticket link carried only by a body."""
        files, descriptions = make_files(), make_descriptions()
        commit = self._commit(
            "tidy the shared address pipe",
            body="Completes DD-42 by moving the length rule into the shared pipe.",
        )
        self.assertTrue(intent.apply_commit(commit, files, descriptions))
        self.assertEqual(files["src/x.ts"]["tickets"], {"DD-42": 1})
        self.assertEqual(descriptions["DD-42"]["count"], 1)
        self.assertEqual(descriptions["DD-42"]["first"], "2024-05-01")

    def test_body_contributes_at_most_three_tickets(self):
        """Catches one release-note body attributing a file to every ticket it lists."""
        files, descriptions = make_files(), make_descriptions()
        commit = self._commit(
            "release candidate",
            body="Rolls up ZZ-9, AA-1, MM-5, BB-2 and CC-3 into the June release.",
        )
        self.assertTrue(intent.apply_commit(commit, files, descriptions))
        self.assertEqual(list(files["src/x.ts"]["tickets"]), ["ZZ-9", "AA-1", "MM-5"])

    def test_subject_ticket_is_not_diluted_by_body_tickets(self):
        """Catches a body mentioning neighbouring work adding links to this file."""
        files, descriptions = make_files(), make_descriptions()
        commit = self._commit(
            "DD-1: add address form",
            body="Also relates to XX-9 and YY-8, which are separate pieces of work.",
        )
        self.assertTrue(intent.apply_commit(commit, files, descriptions))
        self.assertEqual(files["src/x.ts"]["tickets"], {"DD-1": 1})

    def test_junk_subject_falls_back_to_the_body_first_paragraph(self):
        files, descriptions = make_files(), make_descriptions()
        commit = self._commit(
            "DD-9: wip",
            body=(
                "Address entry now rejects lines longer than 35 characters,\n"
                "matching the downstream schema.\n"
                "\n"
                "The postcode lookup is unchanged.\n"
            ),
        )
        intent.apply_commit(commit, files, descriptions)
        self.assertEqual(
            list(descriptions["DD-9"]["descriptions"]),
            [
                "Address entry now rejects lines longer than 35 characters, "
                "matching the downstream schema."
            ],
        )

    def test_body_fallback_description_is_truncated_at_a_word_boundary(self):
        files, descriptions = make_files(), make_descriptions()
        commit = self._commit("DD-9: wip", body=("abcd " * 70).strip())
        intent.apply_commit(commit, files, descriptions)
        stored = list(descriptions["DD-9"]["descriptions"])
        self.assertEqual(stored, [("abcd " * 60).strip()])
        self.assertEqual(len(stored[0]), 299)

    def test_good_subject_description_is_not_replaced_by_the_body(self):
        files, descriptions = make_files(), make_descriptions()
        commit = self._commit(
            "DD-9: introduce address validation rules",
            body="Some other prose entirely, long enough to survive the filter.",
        )
        intent.apply_commit(commit, files, descriptions)
        self.assertEqual(
            list(descriptions["DD-9"]["descriptions"]), ["introduce address validation rules"]
        )


class IndexRepositoryTest(SettingsIsolated):
    def test_end_to_end_over_ndjson(self):
        commits = [
            {
                "repository": "repo-a",
                "subject": "DD-1: add address form",
                "is_merge": False,
                "author_date": "2024-01-02T09:00:00+00:00",
                "files": [{"path": "a.ts"}, {"path": "b.ts"}],
            },
            {
                "repository": "repo-a",
                "subject": "DD-1: add address form",
                "is_merge": False,
                "author_date": "2024-02-03T09:00:00+00:00",
                "files": [{"path": "a.ts"}],
            },
            {
                "repository": "repo-a",
                "subject": "merge branch",
                "is_merge": True,
                "author_date": "2024-02-04T09:00:00+00:00",
                "files": [{"path": "a.ts"}],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ndjson = Path(tmp) / "commits.ndjson"
            ndjson.write_text("\n".join(json.dumps(c) for c in commits) + "\n", encoding="utf-8")
            descriptions = make_descriptions()
            files, seen = intent.index_repository(ndjson, descriptions)

        self.assertEqual(seen, 2)
        self.assertEqual(files["a.ts"]["tickets"], {"DD-1": 2})
        self.assertEqual(files["a.ts"]["first"], "2024-01-02")
        self.assertEqual(files["a.ts"]["last"], "2024-02-03")
        self.assertEqual(descriptions["DD-1"]["descriptions"]["add address form"], 2)


class AutomatedAuthorTest(SettingsIsolated):
    """A body is evidence only if a person wrote it. The rule matches what the
    author *is*, because matching what one bot *says* does not transfer to the
    next estate - Renovate writes "Update dependency X", never "Bumps [".

    The break each test catches is a machine's prose entering the artefacts as a
    ticket link or a description, or - the two "kept" cases - the identity rule
    growing wide enough to discard a person's."""

    BODY = "Completes DD-42 by pinning the newer release across the workspaces."

    def _link(self, **identity):
        files, descriptions = make_files(), make_descriptions()
        commit = make_commit("update the pinned library", body=self.BODY, **identity)
        indexed = intent.apply_commit(commit, files, descriptions)
        return indexed, files

    def test_dependabot_body_without_any_bot_text_is_rejected(self):
        indexed, files = self._link(
            author={"name": "dependabot[bot]", "email": "support@example.example"}
        )
        self.assertFalse(indexed)
        self.assertEqual(len(files), 0)

    def test_github_actions_body_is_rejected(self):
        indexed, files = self._link(
            author={"name": "github-actions[bot]", "email": "actions@example.example"}
        )
        self.assertFalse(indexed)
        self.assertEqual(len(files), 0)

    def test_body_with_a_bot_committer_and_a_human_author_is_rejected(self):
        indexed, files = self._link(
            author=HUMAN,
            committer={"name": "github-actions[bot]", "email": "actions@example.example"},
        )
        self.assertFalse(indexed)
        self.assertEqual(len(files), 0)

    def test_jenkins_body_is_rejected_by_the_explicit_list(self):
        indexed, files = self._link(author={"name": "jenkins", "email": "jenkins@example.example"})
        self.assertFalse(indexed)
        self.assertEqual(len(files), 0)

    def test_human_noreply_github_address_is_kept(self):
        indexed, files = self._link(
            author={"name": "A Person", "email": "123456+aperson@users.noreply.github.com"}
        )
        self.assertTrue(indexed)
        self.assertEqual(files["src/x.ts"]["tickets"], {"DD-42": 1})

    def test_human_prose_naming_a_bot_is_kept(self):
        files, descriptions = make_files(), make_descriptions()
        commit = make_commit(
            "tidy the caching layer",
            body="We renovate the caching layer under DD-42 before the release.",
        )
        self.assertTrue(intent.apply_commit(commit, files, descriptions))
        self.assertEqual(files["src/x.ts"]["tickets"], {"DD-42": 1})

    def test_automated_bodies_are_counted_separately_in_the_report(self):
        commits = [
            make_commit(
                "DD-1: bump the pinned library",
                body=self.BODY,
                author={"name": "dependabot[bot]", "email": "support@example.example"},
            ),
            make_commit(
                "DD-1: run the scheduled workflow",
                body=self.BODY,
                author={"name": "github-actions[bot]", "email": "actions@example.example"},
            ),
            make_commit("DD-1: widen the address field", body=self.BODY),
        ]
        report = intent.BodyReport()
        with tempfile.TemporaryDirectory() as tmp:
            intent.index_repository(write_ndjson(tmp, commits), make_descriptions(), report)
        self.assertEqual(report.automated, 2)


CHECKLIST = "Checklist: tests updated, docs updated, ticket linked."


class ConfigurableAutomationTest(SettingsIsolated):
    """The identity list is matched as a whole word, and several entries are also
    surnames. Measured on one estate it matched 23 identities and every one was a
    machine - but the rule as written discards the bodies of anyone called
    Jenkins, so an estate must be able to narrow it, and a run must say when it
    fired.

    The break each test catches: a person's evidence discarded with no override
    and nothing in the output to notice it by."""

    BODY = "Completes DD-42 by pinning the newer release across the workspaces."

    def _linked(self, **identity):
        files, descriptions = make_files(), make_descriptions()
        commit = make_commit("update the pinned library", body=self.BODY, **identity)
        intent.apply_commit(commit, files, descriptions)
        return files["src/x.ts"]["tickets"] != {}

    def test_a_person_sharing_a_name_with_a_build_server_is_lost_by_default(self):
        # Not a bug being fixed: the documented cost of a whole-word list, pinned
        # so narrowing it stays a deliberate choice rather than a surprise.
        self.assertFalse(self._linked(author={"name": "Bob Jenkins", "email": "b@example.example"}))

    def test_narrowing_the_list_keeps_that_person(self):
        config.AUTOMATION_IDENTITIES = ["renovate", "snyk"]
        self.assertTrue(self._linked(author={"name": "Bob Jenkins", "email": "b@example.example"}))

    def test_emptying_the_list_leaves_only_the_bot_convention(self):
        config.AUTOMATION_IDENTITIES = []
        self.assertTrue(self._linked(author={"name": "jenkins", "email": "j@example.example"}))
        self.assertFalse(
            self._linked(author={"name": "renovate[bot]", "email": "r@example.example"})
        )

    def test_the_list_is_read_when_called_not_when_imported(self):
        # configure() runs after import; a pattern captured at import would ignore it.
        config.AUTOMATION_IDENTITIES = ["jenkins"]
        self.assertFalse(self._linked(author={"name": "jenkins", "email": "j@example.example"}))
        config.AUTOMATION_IDENTITIES = ["renovate"]
        self.assertTrue(self._linked(author={"name": "jenkins", "email": "j@example.example"}))

    def test_the_report_names_who_was_treated_as_automation(self):
        commits = [
            make_commit(
                "DD-1: bump the pinned library",
                body=self.BODY,
                author={"name": "jenkins", "email": "jenkins@example.example"},
            ),
            make_commit("DD-1: widen the address field", body=self.BODY),
        ]
        report = intent.BodyReport()
        with tempfile.TemporaryDirectory() as tmp:
            intent.index_repository(write_ndjson(tmp, commits), make_descriptions(), report)
        self.assertEqual(
            dict(report.automated_identities), {"jenkins <jenkins@example.example>": 1}
        )


class BoilerplateTest(SettingsIsolated):
    """Contributor templates are learned from the repository being indexed, not
    from a shipped pattern list. The break each test catches is boilerplate
    reaching the artefacts as evidence, or the thresholds eating real prose."""

    def _index(self, commits, report=None):
        with tempfile.TemporaryDirectory() as tmp:
            ndjson = write_ndjson(tmp, commits)
            descriptions = make_descriptions()
            files, seen = intent.index_repository(ndjson, descriptions, report)
        return files, seen, descriptions

    def test_line_recurring_in_five_bodies_is_dropped_from_every_one(self):
        commits = [
            make_commit("DD-1: wip", body=f"{CHECKLIST}\n\nWidened field {i} to 35 characters.")
            for i in range(5)
        ]
        _, _, descriptions = self._index(commits)
        self.assertEqual(
            sorted(descriptions["DD-1"]["descriptions"]),
            [f"Widened field {i} to 35 characters." for i in range(5)],
        )

    def test_line_recurring_in_only_four_bodies_is_kept(self):
        commits = [
            make_commit("DD-1: wip", body=f"{CHECKLIST}\n\nWidened field {i} to 35 characters.")
            for i in range(4)
        ]
        _, _, descriptions = self._index(commits)
        self.assertEqual(dict(descriptions["DD-1"]["descriptions"]), {CHECKLIST: 4})

    def test_line_under_two_percent_of_a_large_repository_is_kept(self):
        commits = [
            make_commit("DD-1: wip", body=f"{CHECKLIST}\n\nWidened field {i} to 35 characters.")
            for i in range(5)
        ]
        commits += [
            make_commit("DD-2: wip", body=f"Unrelated change number {i} to the postcode lookup.")
            for i in range(295)
        ]
        _, _, descriptions = self._index(commits)
        self.assertEqual(descriptions["DD-1"]["descriptions"][CHECKLIST], 5)

    def test_boilerplate_is_learned_per_repository(self):
        template = [
            make_commit("DD-1: wip", body=f"{CHECKLIST}\n\nWidened field {i} to 35 characters.")
            for i in range(5)
        ]
        one_off = [make_commit("DD-2: wip", body=CHECKLIST, repo="repo-b")] + [
            make_commit("DD-3: wip", body=f"Postcode lookup change {i} for the search page.")
            for i in range(4)
        ]
        _, _, described_a = self._index(template)
        _, _, described_b = self._index(one_off)
        self.assertNotIn(CHECKLIST, described_a["DD-1"]["descriptions"])
        self.assertEqual(dict(described_b["DD-2"]["descriptions"]), {CHECKLIST: 1})

    def test_body_that_is_only_boilerplate_is_rejected_entirely(self):
        commits = [
            make_commit("tidy the shared pipe", body="Refs DD-77 in the standard release template.")
            for _ in range(5)
        ]
        report = intent.BodyReport()
        files, seen, descriptions = self._index(commits, report)
        self.assertEqual(seen, 0)
        self.assertEqual(len(files), 0)
        self.assertNotIn("DD-77", descriptions)
        self.assertEqual((report.boilerplate_lines, report.boilerplate_emptied), (1, 5))

    def test_counting_ignores_case_and_spacing_but_survivors_keep_their_text(self):
        variants = [
            CHECKLIST,
            "   Checklist: tests   updated, docs updated, ticket linked.   ",
            "CHECKLIST: TESTS UPDATED, DOCS UPDATED, TICKET LINKED.",
            CHECKLIST,
            "Checklist:  tests updated,  docs updated,  ticket linked.",
        ]
        commits = [
            make_commit("DD-1: wip", body=f"{line}\n\nWidened   the ADDRESS field {i} to 35 chars.")
            for i, line in enumerate(variants)
        ]
        _, _, descriptions = self._index(commits)
        self.assertEqual(
            sorted(descriptions["DD-1"]["descriptions"]),
            [f"Widened   the ADDRESS field {i} to 35 chars." for i in range(5)],
        )

    def test_lines_under_eight_characters_never_count_towards_boilerplate(self):
        commits = [
            make_commit("DD-1: wip", body=f"Done.\nOn review.\nAddress field {i} widened to 35.")
            for i in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            counts, bodies = intent.count_body_lines(write_ndjson(tmp, commits))
        self.assertEqual(bodies, 5)
        self.assertNotIn("done.", counts)
        self.assertEqual(counts["on review."], 5)
        self.assertEqual(intent.learn_boilerplate(counts, bodies), frozenset({"on review."}))


GOOD_SUBJECT = "DD-1: introduce address validation rules"
PROSE = "Address entry now rejects lines longer than 35 characters, matching the schema."


class EvidenceFieldsTest(SettingsIsolated):
    """`s` and `b` carry the commit's own words as written: every subject,
    including the weak ones a curated description discards, and body prose even
    when the subject is serviceable.

    The break each test catches is primary evidence never reaching the artefact.
    A single description field can only hold one of the two, so a body behind a
    working subject and a subject the junk filter rejects were both unreachable -
    and the grounding contract admits commit subjects and bodies as evidence."""

    def _artefact(self, commits, repo="repo-a"):
        """The committed ticket-descriptions artefact, built by the real stage."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history" / repo
            history.mkdir(parents=True)
            write_ndjson(history, commits)
            config.configure(
                HISTORY_DIR=root / "history",
                INTENT_INDEX_PATH=root / "file-tickets.json.gz",
                TICKET_DESCRIPTIONS_PATH=root / "ticket-descriptions.json.gz",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(intent.main(), 0)
            return json.load(gzip.open(config.TICKET_DESCRIPTIONS_PATH, "rt", encoding="utf-8"))

    def test_junk_subject_discarded_by_d_is_kept_verbatim_in_s(self):
        ticket = self._artefact([make_commit("DD-1: wip")])["DD-1"]
        self.assertEqual(ticket["d"], [])
        self.assertEqual(ticket["s"], ["wip"])

    def test_a_bare_ticket_reference_is_never_stored_as_a_subject(self):
        """Stripping the reference from a subject that is only a reference leaves
        the empty string. An empty string is not evidence, and a field of empty
        strings would read as though it were."""
        artefact = self._artefact(
            [
                make_commit("DD-1"),
                make_commit("DD-2"),
                make_commit("DD-2: widen the address field"),
            ]
        )
        self.assertNotIn("s", artefact["DD-1"])
        self.assertNotIn("b", artefact["DD-1"])
        self.assertEqual(artefact["DD-2"]["s"], ["widen the address field"])

    def test_body_is_kept_when_the_subject_is_perfectly_good(self):
        # The point of the change: a fallback can never surface a body that sits
        # behind a serviceable subject, and most bodies do.
        ticket = self._artefact([make_commit(GOOD_SUBJECT, body=PROSE)])["DD-1"]
        self.assertEqual(ticket["d"], ["introduce address validation rules"])
        self.assertEqual(ticket["b"], [PROSE])

    def test_d_is_unchanged_for_a_good_subject_and_for_a_junk_one(self):
        """The compatibility guarantee: `d` stays subject-first, body-as-fallback,
        junk filtered. The explorer and the skills read it."""
        artefact = self._artefact(
            [
                make_commit(GOOD_SUBJECT, body=PROSE),
                make_commit("DD-2: wip", body=PROSE),
                make_commit("DD-3: wip"),
            ]
        )
        self.assertEqual(artefact["DD-1"]["d"], ["introduce address validation rules"])
        self.assertEqual(artefact["DD-2"]["d"], [PROSE])
        self.assertEqual(artefact["DD-3"]["d"], [])

    def test_s_deduplicates_and_caps_at_three_most_frequent_first(self):
        counts = {"widen the address field": 4, "correct the postcode lookup": 3, "tidy up": 2}
        commits = [
            make_commit(f"DD-1: {subject}") for subject, n in counts.items() for _ in range(n)
        ]
        commits.append(make_commit("DD-1: revert the release branch"))
        self.assertEqual(self._artefact(commits)["DD-1"]["s"], list(counts))

    def test_b_deduplicates_and_caps_at_two_most_frequent_first(self):
        bodies = {
            f"Change number {i} to the address entry rules, which is prose.": 3 - i
            for i in range(3)
        }
        commits = [
            make_commit("DD-1: wip", body=body) for body, n in bodies.items() for _ in range(n)
        ]
        self.assertEqual(self._artefact(commits)["DD-1"]["b"], list(bodies)[:2])

    def test_b_truncates_at_a_word_boundary(self):
        ticket = self._artefact([make_commit("DD-1: wip", body=("abcd " * 900).strip())])["DD-1"]
        self.assertEqual(ticket["b"], [("abcd " * 800).strip()])
        self.assertEqual(len(ticket["b"][0]), 3999)

    def test_a_nine_hundred_character_body_is_whole_in_b_and_a_label_in_d(self):
        """The asymmetry the two caps exist to express, and the regression that
        matters if someone later tidies them back into one constant: `d` is a
        label a consumer renders, `b` is the evidence, and the rationale in a body
        is exactly what a label-sized cut removes."""
        body = ("word " * 180).strip()
        self.assertEqual(len(body), 899)
        ticket = self._artefact([make_commit("DD-1: wip", body=body)])["DD-1"]
        self.assertEqual(ticket["b"], [body])
        self.assertEqual(ticket["d"], [("word " * 60).strip()])
        self.assertEqual(len(ticket["d"][0]), 299)

    def test_a_bot_authored_body_is_absent_from_b(self):
        ticket = self._artefact(
            [
                make_commit(
                    "DD-1: bump the pinned library",
                    body=PROSE,
                    author={"name": "dependabot[bot]", "email": "support@example.example"},
                )
            ]
        )["DD-1"]
        self.assertNotIn("b", ticket)
        self.assertEqual(ticket["s"], ["bump the pinned library"])

    def test_a_learned_boilerplate_line_is_absent_from_b(self):
        commits = [
            make_commit("DD-1: wip", body=f"{CHECKLIST}\n\nWidened field {i} to 35 characters.")
            for i in range(5)
        ]
        ticket = self._artefact(commits)["DD-1"]
        self.assertEqual(
            ticket["b"],
            ["Widened field 0 to 35 characters.", "Widened field 1 to 35 characters."],
        )

    def test_equal_counts_order_by_text_not_by_insertion(self):
        """Two runs on the same inputs must be byte-identical, so equal counts
        cannot fall back to insertion or hash order."""
        subjects = [
            "zebra diagram added to the guidance",
            "middle of the alphabet, address rules",
            "apple pie ordering on the summary page",
        ]
        commits = [make_commit(f"DD-1: {subject}") for subject in subjects]
        self.assertEqual(self._artefact(commits)["DD-1"]["s"], sorted(subjects))


if __name__ == "__main__":
    unittest.main()
