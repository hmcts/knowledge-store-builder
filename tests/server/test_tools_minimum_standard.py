"""The minimum-standard tool (spec, Tools): `minimum_standard`.

Every expected value is a literal from DESIGN.md 11, or the fixture's records as
knowledge-store-builder's writer wrote them, which `build.py` holds as literals.
A served record is that record less its stamp, with `reason` null on a point that
had none. Each store is the synthetic store, built for real and opened through the
real index; the only stand-ins are the clock and the cache directory.
"""

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from fixture_store.build import MINIMUM_STANDARD, MINIMUM_STANDARD_RECORDS, build
from knowledgestore.server import results, store, tools_graph, tools_minimum_standard
from knowledgestore.server.tools_graph import ArgumentRefused

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

LAYER = "minimum_standard"
BY_REPO = f"{MINIMUM_STANDARD}/by-repo"

# DESIGN.md 11: the summary's own note.
NOTES = ["archived repositories are not in the store"]

# The keys each level of an answer carries, and no others.
ITEM_KEYS = {"repository", "as_of", "settings_read_at", "points"}
POINT_KEYS = {"state", "evidence", "reason"}
FINDING_KEYS = {"line", "path", "rule"}
# A key that would make the answer a verdict rather than evidence.
VERDICTS = {"pass", "passed", "passes", "score", "scores", "compliant", "verdict", "overall"}

# DESIGN.md 11: payments-api's one secret-scan finding, a location and a rule.
PAYMENTS_FINDING = {"line": 4, "path": "config/application.yml", "rule": "key-name"}
# DESIGN.md 11: reporting-worker's key file, reported as a whole file, so it has no line.
KEY_FILE_FINDING = {"path": "deploy/signing.pem", "rule": "key-file"}

UNSTAMPED = (
    "minimum_standard layer present but format unrecognised (found none, supports 1.x); not read"
)

UNKNOWN = {
    "answer": "not in the estate",
    "exact": None,
    "near_misses": [
        {"repository": "payments-api", "near_miss": True},
        {"repository": "payment-apis", "near_miss": True},
    ],
}


def known(name: str) -> dict:
    return {"answer": "in the estate", "exact": name, "matched_case_insensitively": False}


def served(name: str) -> dict:
    """The record `build.py` holds for `name`, as the tool serves it."""
    record = MINIMUM_STANDARD_RECORDS[name]
    return {
        "repository": name,
        "as_of": record["as_of"],
        "settings_read_at": record["settings_read_at"],
        "points": {
            key: {
                "state": point["state"],
                "evidence": point["evidence"],
                "reason": point.get("reason"),
            }
            for key, point in record["points"].items()
        },
    }


def envelope(layers_used: list[str]) -> dict:
    return {
        "store": {
            "id": "example-store",
            "format": {"major": 1, "minor": 0},
            "library_version": "0.0.0+fixture",
            "sources_as_of": "2026-10-15T23:30:00-02:00",
            "index": "present",
            "layers_used": layers_used,
            "notice": None,
        }
    }


def keys(value: Any) -> Iterator[str]:
    """Every key at every depth of `value`."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from keys(inner)


def rewrite(root: Path, name: str, change) -> None:
    """Apply `change` to `name`'s by-repo file in place."""
    path = root / BY_REPO / f"{name}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")


class MinimumStandardTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cache = self.tmp / "cache"
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, prepare=None, **options) -> store.Session:
        options.setdefault("minimum_standard", True)
        root = build(self.tmp / f"s-{len(list(self.tmp.iterdir()))}", **options)
        if prepare is not None:
            prepare(root)
        session = store.Session(lambda: store.open_store(root, self.cache), lambda: NOW)
        self.addCleanup(lambda: session.current()[0].close())
        return session


class ScopeTest(MinimumStandardTestCase):
    def test_named_repositories_return_their_points_citations_and_as_of(self):
        """Catches: a record rebuilt rather than served as the library wrote it, a
        point, citation, `as_of` or `settings_read_at` lost, or an answer that does
        not say each name was looked up (DESIGN.md 11)."""
        self.assertEqual(
            tools_minimum_standard.minimum_standard(self.session(), repos="payments-api"),
            {
                **envelope(["index", LAYER]),
                "scope": "repos",
                "repositories": [known("payments-api")],
                "meanings": tools_minimum_standard.MEANINGS,
                "notes": NOTES,
                "items": [served("payments-api")],
                "total": 1,
                "page": 1,
                "next_page": None,
            },
        )

    def test_several_names_are_one_list_sorted_with_each_lookup_in_the_order_asked(self):
        """Catches: only the first of several names read, a name's surrounding
        spaces taken as part of it, or the items left in the order asked."""
        found = tools_minimum_standard.minimum_standard(
            self.session(), repos="reporting-worker, payments-api"
        )
        self.assertEqual(found["repositories"], [known("reporting-worker"), known("payments-api")])
        self.assertEqual(found["items"], [served("payments-api"), served("reporting-worker")])

    def test_a_list_of_names_answers_as_the_same_names_comma_separated(self):
        """Catches: a list read as one name, only its first element read, or its
        names looked up in another order or form than the comma-separated text
        that callers on v0.2.1 send."""
        session = self.session()
        names = ["reporting-worker", "payments-api", "payment-api"]
        self.assertEqual(
            tools_minimum_standard.minimum_standard(session, repos=names),
            tools_minimum_standard.minimum_standard(session, repos=", ".join(names)),
        )

    def test_a_list_element_holding_a_comma_is_one_unknown_name_not_two(self):
        """Catches: list elements split on commas as the text is, so a name that is
        not a repository's is quietly read as two that are."""
        found = tools_minimum_standard.minimum_standard(
            self.session(), repos=["payments-api,ledger-service"]
        )
        self.assertEqual(len(found["repositories"]), 1)
        self.assertNotEqual(found["repositories"][0].get("exact"), "payments-api")
        self.assertEqual(found["items"], [])

    def test_at_most_a_hundred_names_in_either_form(self):
        """Catches: the name limit applied to the text and not the list (or the
        reverse), or moved off by one for either."""
        session = self.session()
        allowed = [f"repo-{n}" for n in range(tools_minimum_standard.MAX_REPOS)]
        too_many = [*allowed, "one-more"]
        for repos in (allowed, ",".join(allowed)):
            with self.subTest(allowed=type(repos)):
                found = tools_minimum_standard.minimum_standard(session, repos=repos)
                self.assertEqual(len(found["repositories"]), 100)
        for repos in (too_many, ",".join(too_many)):
            with self.subTest(too_many=type(repos)):
                with self.assertRaisesRegex(ArgumentRefused, "at most 100 repositories"):
                    tools_minimum_standard.minimum_standard(session, repos=repos)

    def test_a_list_that_is_not_all_text_is_refused_naming_the_forms(self):
        """Catches: a non-string element looked up as its text or dropped, or a
        refusal that does not say what `repos` accepts."""
        session = self.session()
        cases: list[Any] = [["payments-api", 5], [None], [["payments-api"]], 5, {"a": 1}]
        for repos in cases:
            with self.subTest(repos=repos):
                with self.assertRaisesRegex(
                    ArgumentRefused,
                    "^repos must be a list of repository names"
                    r" \(a comma-separated string is also accepted\)$",
                ):
                    tools_minimum_standard.minimum_standard(session, repos=repos)

    def test_a_repository_in_the_estate_without_a_record_is_named_not_dropped(self):
        """Catches: a repository the layer holds no record for answered as an empty
        list with nothing to say why, which reads as "nothing to report"."""
        found = tools_minimum_standard.minimum_standard(self.session(), repos="accounts-web")
        self.assertEqual(found["repositories"], [known("accounts-web")])
        self.assertEqual(found["no_record"], ["accounts-web"])
        self.assertEqual(found["items"], [])
        self.assertEqual(
            found["searched"], "searched 12 repositories in the index and minimum_standard layers"
        )

    def test_a_pattern_scopes_by_name(self):
        """Catches: a pattern matched as a substring or a regular expression rather
        than as a glob over the names the layer holds (DESIGN.md 11)."""
        session = self.session()
        for pattern, names in (
            ("pay*", ["payments-api"]),
            ("*-service", ["ledger-service"]),
            ("*", ["ledger-service", "payments-api", "reporting-worker"]),
            ("payments", []),
        ):
            with self.subTest(pattern=pattern):
                found = tools_minimum_standard.minimum_standard(session, pattern=pattern)
                self.assertEqual([item["repository"] for item in found["items"]], names)
                self.assertEqual(found["scope"], "pattern")
                self.assertEqual(found["store"]["layers_used"], [LAYER])

    def test_a_product_scopes_to_the_repositories_the_summary_declares(self):
        """Catches: a product resolved by name similarity, or from anything but the
        summary's `products` map (DESIGN.md 11: payments is ledger-service and
        payments-api, and not reporting-worker)."""
        self.assertEqual(
            tools_minimum_standard.minimum_standard(self.session(), product="payments"),
            {
                **envelope([LAYER]),
                "scope": "product",
                "product": {
                    "answer": "a declared product",
                    "exact": "payments",
                    "repositories": ["ledger-service", "payments-api"],
                },
                "meanings": tools_minimum_standard.MEANINGS,
                "notes": NOTES,
                "items": [served("ledger-service"), served("payments-api")],
                "total": 2,
                "page": 1,
                "next_page": None,
            },
        )

    def test_no_scope_is_the_whole_layer(self):
        found = tools_minimum_standard.minimum_standard(self.session())
        self.assertEqual(found["scope"], "estate")
        self.assertEqual(
            found["items"],
            [served("ledger-service"), served("payments-api"), served("reporting-worker")],
        )
        self.assertEqual(found["total"], 3)

    def test_more_than_one_scope_is_refused(self):
        """Catches: two scopes silently combined, or one silently ignored."""
        session = self.session()
        cases: list[dict[str, Any]] = [
            {"repos": "payments-api", "product": "payments"},
            {"repos": "payments-api", "pattern": "pay*"},
            {"pattern": "pay*", "product": "payments"},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ArgumentRefused):
                    tools_minimum_standard.minimum_standard(session, **arguments)

    def test_an_empty_scope_is_refused(self):
        """Catches: `repos` of only commas or spaces read as no scope, which would
        answer for the whole estate."""
        session = self.session()
        cases: list[dict[str, Any]] = [{"repos": " , "}, {"pattern": ""}, {"product": " "}]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ArgumentRefused):
                    tools_minimum_standard.minimum_standard(session, **arguments)


class NearMissTest(MinimumStandardTestCase):
    def test_an_unknown_repository_gives_near_misses_and_no_record(self):
        """Catches: a near miss answered in place of the name asked (DESIGN.md 2)."""
        found = tools_minimum_standard.minimum_standard(self.session(), repos="payment-api")
        self.assertEqual(found["repositories"], [UNKNOWN])
        self.assertEqual(found["items"], [])
        self.assertNotIn("no_record", found)

    def test_an_unknown_product_gives_near_misses_and_no_record(self):
        """Catches: an unknown product answered as the whole estate, or as an empty
        product, or the nearest product answered in its place."""
        found = tools_minimum_standard.minimum_standard(self.session(), product="payment")
        self.assertEqual(
            found["product"],
            {
                "answer": "not a declared product",
                "exact": None,
                "near_misses": [{"product": "payments", "near_miss": True}],
            },
        )
        self.assertEqual(found["items"], [])
        self.assertEqual(
            found["searched"], "searched 12 repositories in the minimum_standard layer"
        )

    def test_a_product_in_a_store_that_declares_none(self):
        def undeclare(root: Path) -> None:
            path = root / MINIMUM_STANDARD / "summary.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            del document["products"]
            path.write_text(json.dumps(document), encoding="utf-8")

        found = tools_minimum_standard.minimum_standard(
            self.session(prepare=undeclare), product="payments"
        )
        self.assertEqual(
            found["product"], {"answer": "no products declared", "exact": None, "near_misses": []}
        )
        self.assertEqual(found["items"], [])


class StampTest(MinimumStandardTestCase):
    def test_an_unrecognised_stamp_is_refused_with_the_reason(self):
        """Catches: a layer with no stamp, another layer's or another major's, read
        as present or as empty-and-fine (DESIGN.md 11)."""
        for stamp, found in (
            (None, "none"),
            ({"layer": LAYER, "major": 2, "minor": 0}, "2.0"),
            ({"layer": "relationships", "major": 1, "minor": 0}, "a stamp for the relationships layer"),
        ):  # fmt: skip
            with self.subTest(found=found):
                session = self.session(minimum_standard_format=stamp)
                with self.assertRaises(results.LayerUnrecognised) as raised:
                    tools_minimum_standard.minimum_standard(session, repos="payments-api")
                self.assertEqual(
                    str(raised.exception),
                    f"{LAYER} layer present but format unrecognised "
                    f"(found {found}, supports 1.x); not read",
                )

    def test_a_newer_minor_is_read(self):
        session = self.session(minimum_standard_format={"layer": LAYER, "major": 1, "minor": 7})
        found = tools_minimum_standard.minimum_standard(session, repos="payments-api")
        self.assertEqual(found["items"], [served("payments-api")])

    def test_store_info_lists_the_unrecognised_layer_with_the_reason(self):
        info = tools_graph.store_info(self.session(minimum_standard_format=None))
        self.assertEqual(info["layers"][LAYER], {"present": True, "unrecognised": UNSTAMPED})
        healthy = tools_graph.store_info(self.session())
        self.assertEqual(healthy["layers"][LAYER], {"present": True})

    def test_an_absent_layer_is_said_to_be_absent(self):
        """Catches: a store without the layer answered as an empty list that reads
        as "nothing found" (DESIGN.md 9.4)."""
        session = self.session(minimum_standard=False)
        found = tools_minimum_standard.minimum_standard(session)
        self.assertEqual(found["items"], [])
        self.assertEqual(
            found["searched"], "searched no layer; the minimum_standard layer is absent"
        )
        named = tools_minimum_standard.minimum_standard(session, repos="payments-api")
        self.assertEqual(named["repositories"], [known("payments-api")])
        self.assertEqual(
            named["searched"],
            "searched 12 repositories in the index layer; the minimum_standard layer is absent",
        )

    def test_a_record_with_another_stamp_is_not_read_and_is_named(self):
        """Catches: one by-repo file in another format served as this layer's."""

        def foreign(root: Path) -> None:
            rewrite(root, "payments-api", lambda d: d.update(format={"layer": LAYER, "major": 2}))

        found = tools_minimum_standard.minimum_standard(self.session(prepare=foreign))
        self.assertEqual(
            [item["repository"] for item in found["items"]], ["ledger-service", "reporting-worker"]
        )
        self.assertEqual(
            found["records_not_read"],
            {
                "repositories": ["payments-api"],
                "reason": tools_minimum_standard.RECORD_NOT_READ,
            },
        )


class EvidenceNotVerdictTest(MinimumStandardTestCase):
    def test_every_level_carries_exactly_its_keys_and_never_a_verdict(self):
        """Catches: a pass, score or overall verdict computed by the server, or one
        a store wrote passed through (DESIGN.md 11)."""

        def verdicts(root: Path) -> None:
            def add(document: dict) -> None:
                document.update(passes=True, score=7, overall="compliant")
                for point in document["points"].values():
                    point.update(passed=True, score=1)

            for name in MINIMUM_STANDARD_RECORDS:
                rewrite(root, name, add)

        session = self.session(prepare=verdicts)
        cases: list[dict[str, Any]] = [{}, {"repos": "payments-api"}, {"product": "payments"}]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                found = tools_minimum_standard.minimum_standard(session, **arguments)
                self.assertEqual(VERDICTS & set(keys(found)), set())
                self.assertTrue(found["items"])
                for item in found["items"]:
                    self.assertEqual(set(item), ITEM_KEYS)
                    self.assertEqual(
                        set(item["points"]),
                        set(MINIMUM_STANDARD_RECORDS[item["repository"]]["points"]),
                    )
                    for point in item["points"].values():
                        self.assertEqual(set(point), POINT_KEYS)

    def test_every_answer_says_not_determinable_is_not_absent_and_is_dated(self):
        """Catches: the meanings dropped from an answer, so `not_determinable` is
        read as a failure, or a record read as true of today."""
        meanings = tools_minimum_standard.MEANINGS
        self.assertIn("not_determinable is not absent", meanings["not_determinable"])
        self.assertIn("not that the team does not do it", meanings["absent"])
        self.assertIn("point in time", meanings["as_of"])
        self.assertIn("no pass, score or overall verdict", meanings["evidence"])
        session = self.session()
        absent = self.session(minimum_standard=False)
        for found in (
            tools_minimum_standard.minimum_standard(session),
            tools_minimum_standard.minimum_standard(session, repos="payment-api"),
            tools_minimum_standard.minimum_standard(session, product="payment"),
            tools_minimum_standard.minimum_standard(absent),
        ):
            self.assertEqual(found["meanings"], meanings)

    def test_a_secret_finding_is_a_rule_a_path_and_a_line_and_never_a_value(self):
        """Catches: a value, a line of the file or any other field of a finding
        passed through, so a secret reaches the answer (DESIGN.md 11); among them a
        `reason`, which other points' citations keep, so a finding filtered as a
        citation would carry the line out."""
        value = "fixture-not-a-secret"

        def leak(root: Path) -> None:
            def add(document: dict) -> None:
                finding = document["points"]["no_secrets"]["evidence"][0]
                finding.update(
                    value=value, text=f"password: {value}", match=value, reason=f"password: {value}"
                )

            rewrite(root, "payments-api", add)

        found = tools_minimum_standard.minimum_standard(
            self.session(prepare=leak), repos="payments-api"
        )
        evidence = found["items"][0]["points"]["no_secrets"]["evidence"]
        self.assertEqual(evidence, [PAYMENTS_FINDING])
        self.assertNotIn(value.encode("utf-8"), results.encode(found))

    def test_a_key_file_finding_has_no_line_and_its_point_is_absent(self):
        """Catches: a finding without a `line` dropped as malformed, or given a null or
        made-up line, or its point's `absent` lost, so a private key committed to a
        repository is answered as nothing found (DESIGN.md 11)."""
        found = tools_minimum_standard.minimum_standard(self.session(), repos="reporting-worker")
        self.assertEqual(
            found["items"][0]["points"]["no_secrets"],
            {
                "state": "absent",
                "evidence": [KEY_FILE_FINDING],
                "reason": (
                    "pattern scan of the current tree found these; values are not "
                    "recorded and history is not scanned"
                ),
            },
        )

    def test_other_evidence_keeps_only_the_layers_citation_fields(self):
        """Catches: an unknown evidence field passed through from a point other than
        no_secrets, where a value could ride as well."""

        def leak(root: Path) -> None:
            def add(document: dict) -> None:
                for point in document["points"].values():
                    for entry in point["evidence"]:
                        entry["value"] = "leaked"

            rewrite(root, "payments-api", add)

        found = tools_minimum_standard.minimum_standard(
            self.session(prepare=leak), repos="payments-api"
        )
        self.assertEqual(found["items"], [served("payments-api")])
        self.assertNotIn(b"leaked", results.encode(found))


class PagingTest(MinimumStandardTestCase):
    def test_pages_stay_within_the_cap_and_reassemble_to_the_layer(self):
        """Catches: the whole estate returned in one response over the cap, an item
        cut, or one lost or repeated between pages."""
        cap = 6 * 1024
        session = self.session()
        pages = [tools_minimum_standard.minimum_standard(session, cap_bytes=cap)]
        while pages[-1]["next_page"] is not None:
            pages.append(
                tools_minimum_standard.minimum_standard(
                    session, cursor=pages[-1]["next_page"], cap_bytes=cap
                )
            )
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(len(results.encode(page)), cap)
            self.assertNotIn("oversize", page)
            self.assertEqual(page["total"], 3)
        self.assertEqual(
            [item for page in pages for item in page["items"]],
            [served("ledger-service"), served("payments-api"), served("reporting-worker")],
        )


if __name__ == "__main__":
    unittest.main()
