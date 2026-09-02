"""Dictionary-encoding the explorer's data block (#245).

The data block is the largest thing on the page and most of its bytes are
repeats of strings already in it, so each column's values are replaced by
indices into a per-column table wherever that costs fewer bytes.

The rule under test is one inequality and nothing else:

    field_bytes - (table_bytes + reference_bytes) > 0, against
    frequency-ordered indices

Two rules that read as obvious were published against this issue and retracted,
and each has a test below because each would have shipped a wrong page:

- "intern when the distinct-to-total ratio is low" - a column with ONE distinct
  value across tens of thousands of rows is the most repetitive column it is
  possible to have and still loses when the value is no longer than its index;
- "never intern a numeric column" - 13-digit epoch milliseconds with a handful
  of distinct values interns by a wide margin.

The other thing these tests defend is that the decision is taken per column
from the page's own data. A curated list of column names, however well measured,
leaves another estate's bulk untouched while still reporting a healthy saving on
the estate it was tuned against.
"""

from __future__ import annotations

import contextlib
import io
import json
import random
import re
import string
import tempfile
import unittest
from collections import Counter
from itertools import product
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import build_explorer as explorer  # noqa: E402
from knowledgestore import io as store_io  # noqa: E402


def rows_carrying(column: int, values: list) -> list:
    """Full-width page rows carrying `values` in `column`, one value per row.

    Full width because the costing names the column it reports, and a short row
    would be costing a column the page does not have.
    """
    blank: list = ["", "", "", "", "", 0, [], [], 0, ""]
    rows = []
    for value in values:
        row = list(blank)
        row[column] = value
        rows.append(row)
    return rows


def two_character_values(count: int) -> list[str]:
    """`count` distinct two-character strings, in a fixed order."""
    alphabet = string.ascii_lowercase + string.digits
    return ["".join(pair) for pair in list(product(alphabet, repeat=2))[:count]]


def sample_rows() -> list[list]:
    """Forty page rows carrying both verdicts, so neither is asserted vacuously.

    Every label is distinct and long, so the label column loses; the repository,
    community label and kind columns repeat heavily, so they win. A change that
    interned everything, or nothing, fails the assertions below rather than
    passing them quietly.

    **Called per test, never shared between them.** These rows were a class
    attribute once, and `encode_rows` mutating its argument in place then went
    undetected: the test that exists to catch it snapshotted rows an
    alphabetically earlier test had already replaced with indices, so it passed
    in the suite and failed only when run alone. The mutation was still caught,
    by a different test - which is worse than not being caught, because the
    mapping from break to observer was wrong and read as right.
    """
    return [
        [
            f"ServiceComponentWithALongDistinctName{index:04d}",
            f"repo-{index % 4}",
            f"src/area{index % 8}/module.ts",
            f"Community area {index % 4}",
            "code",
            40 - index,
            [f"ServiceComponentWithALongDistinctName{(index + 1) % 40:04d}"],
            [f"TICKET-{index % 5}"],
            index % 4,
            "",
        ]
        for index in range(40)
    ]


def optimal_reference_bytes(values: list) -> int:
    """The least the indices can cost, in closed form and independent of the encoder.

    The rearrangement inequality's minimum written out: frequencies sorted
    descending, paired with index widths ascending - one digit for the first ten
    indices, two for the next ninety, and so on. It shares no code with
    `frequency_table`, so the two agreeing is a check rather than a restatement.
    """
    counts = sorted(Counter(map(explorer.json_text, values)).values(), reverse=True)
    return sum(frequency * len(str(index)) for index, frequency in enumerate(counts))


def cheap_screen_rejects(item: explorer.Interning) -> bool:
    """The screen this library deliberately does NOT implement.

    `mean_value_bytes < digits(distinct_count) + 1`, as published: if the average
    value is shorter than the reference that would replace it, the reference term
    alone exceeds the field's bytes. It reads two summary statistics and needs no
    per-value pass, which is the whole attraction.

    It lives in the tests rather than in the encoder because it is not sound
    under frequency ordering, and `test_the_cheap_screen_would_reject_a_column_
    the_inequality_wins` is the counterexample. Since the encoder already holds
    every value in memory, the full costing is one linear pass and the screen
    buys nothing worth a wrong verdict.
    """
    mean = item.field_bytes / item.occurrences
    return mean < len(str(item.distinct)) + 1


class CostModelTest(unittest.TestCase):
    """The inequality, applied to columns whose verdict is arithmetic."""

    def cost(self, values: list, column: int = 0) -> explorer.Interning:
        return explorer.column_interning(rows_carrying(column, values), column)

    def test_a_near_unique_column_is_declined(self):
        """Breaks if the table term leaves the inequality.

        200 distinct values across 200 rows: the table is a second copy of every
        value and the indices are pure addition, so interning makes the block
        bigger. Without the table term the same column reads as a win, which is
        the failure mode that grows the page it was added to shrink.
        """
        item = self.cost([f"Component{index:03d}" for index in range(200)])

        self.assertFalse(item.interned)
        self.assertEqual(item.distinct, 200)
        # Re-derived by hand: 200 x 14 value bytes, a table that repeats all of
        # them plus 199 two-byte separators, two brackets and the column's key,
        # and 490 bytes of indices (10 of one digit, 90 of two, 100 of three).
        self.assertEqual(item.field_bytes, 2800)
        self.assertEqual(item.table_bytes, 2800 + 2 * 199 + 2 + len('"0": ') + 2)
        self.assertEqual(item.reference_bytes, 10 + 90 * 2 + 100 * 3)
        self.assertEqual(item.saving, 2800 - item.table_bytes - 490)

    def test_a_repetitive_long_valued_column_is_interned(self):
        """Breaks if the inequality is inverted, or if interning is switched off.

        Three distinct 40-byte paths across 300 rows. The table costs three
        copies, the indices cost one byte each, and 297 copies of a 40-byte path
        stop being written.
        """
        paths = [f"src/very/long/module/path/number-{index}.ts" for index in range(3)]
        item = self.cost([paths[index % 3] for index in range(300)])

        self.assertTrue(item.interned)
        self.assertEqual(item.distinct, 3)
        self.assertEqual(item.reference_bytes, 300)  # every index is one digit
        self.assertEqual(item.saving, item.field_bytes - item.table_bytes - 300)

    def test_one_distinct_value_across_thousands_of_rows_can_still_lose(self):
        """Breaks if a distinct-to-total ratio replaces the inequality.

        The published-and-retracted rule, mechanised. Both columns here have a
        ratio that rounds to 0.00 - the most repetitive shape a column can have -
        and they split. A single-digit number in every one of 30,000 rows loses,
        because a one-byte value cannot be beaten by a one-byte index and the
        table is pure cost; 200 long paths over the same 30,000 rows win by a
        wide margin. No ratio test can separate them even in principle.
        """
        constant = self.cost([0] * 30_000)
        repeated = self.cost(
            [f"src/module/path/number-{index:03d}.ts" for index in range(200)] * 150
        )

        self.assertLess(constant.distinct / constant.occurrences, 0.005)
        self.assertLess(repeated.distinct / repeated.occurrences, 0.01)
        self.assertFalse(constant.interned)
        self.assertTrue(repeated.interned)
        # Nothing is saved per row - value and index are one byte each - so the
        # loss is exactly the table nobody needed.
        self.assertEqual(constant.field_bytes, constant.reference_bytes)
        self.assertEqual(constant.saving, -constant.table_bytes)

    def test_a_numeric_column_of_wide_values_is_interned(self):
        """Breaks if a "never intern a numeric column" filter is added.

        The second published-and-retracted rule. Epoch milliseconds are 13 digits
        and five distinct values index in one, so the column interns by a wide
        margin though every value in it is a number. "Numeric" was standing in
        for "the value is no longer than its index", which is a property of the
        data and is what the inequality reads.
        """
        stamps = [1_739_923_200_000 + offset for offset in range(5)]
        item = self.cost([stamps[index % 5] for index in range(10_000)])

        self.assertTrue(item.interned)
        self.assertEqual(item.field_bytes, 10_000 * 13)
        self.assertEqual(item.reference_bytes, 10_000)
        self.assertGreater(item.saving, 100_000)

    def test_the_cheap_screen_would_reject_a_column_the_inequality_wins(self):
        """Breaks if the cheap screen is ever wired into the encoder.

        A column that is almost entirely one repeated value, with a thousand rare
        values behind it. Its mean value is shorter than the widest index, so the
        published screen rejects it without costing it - and the costing interns
        it, because frequency ordering gives the dominant value a single-digit
        index. Nothing in the column's summary statistics predicts that; only the
        costing does, which is why the inequality is the contract and every
        screen is an optimisation.
        """
        # Sliced past the first, so the dominant value is not also a rare one:
        # 1,001 distinct values, whose widest index is four digits.
        rare = two_character_values(1_001)[1:]
        item = self.cost(["aa"] * 20_000 + rare)

        self.assertEqual(item.distinct, 1_001)

        self.assertTrue(cheap_screen_rejects(item))
        self.assertTrue(item.interned)
        self.assertGreater(item.saving, 0)

    def test_references_are_costed_over_the_flattened_values_of_a_list_column(self):
        """Breaks if a list column's references are costed per row.

        The dominant column of the estate this work is for holds several values
        per row. Costing one reference per row understates the reference term by
        the mean list length, which overstates the saving - and overstating the
        saving is how a costing predicts wins that are losses.
        """
        tickets = [[f"AAA-{index}", f"BBB-{index}", "SHARED-1"] for index in range(10)]
        rows = rows_carrying(0, [""] * 10)
        for row, cell in zip(rows, tickets):
            row[6] = cell
        item = explorer.column_interning(rows, 6)

        self.assertEqual(item.occurrences, 30)
        self.assertEqual(item.distinct, 21)
        self.assertEqual(
            item.reference_bytes,
            explorer.reference_bytes([v for c in tickets for v in c], item.table),
        )


class IndexAssignmentTest(unittest.TestCase):
    """Frequency ordering, and the determinism of the table it produces."""

    def test_the_indices_cost_the_least_any_assignment_can(self):
        """Breaks if indices are assigned in any order but frequency order.

        Including the order the values first appear in, which is what a first cut
        ships and which the permutation attack below cannot see: first appearance
        in a shuffled multiset already correlates with frequency, so every random
        permutation is worse than it and none refutes it. Only a cost derived
        independently of the encoder separates the two, and on this column a
        first-appearance assignment costs measurably more.
        """
        rng = random.Random(1245)
        values = [f"value-{index}" for index in range(120) for _ in range(120 - index)]
        rng.shuffle(values)

        table = explorer.frequency_table(values)
        first_appearance = tuple(dict.fromkeys(values))
        self.assertEqual(explorer.reference_bytes(values, table), optimal_reference_bytes(values))
        self.assertLess(
            explorer.reference_bytes(values, table),
            explorer.reference_bytes(values, first_appearance),
        )

    def test_no_other_index_assignment_beats_frequency_ordering(self):
        """Breaks if indices are assigned in arrival or alphabetical order.

        Reference cost is a sum of products with one sequence fixed, so by the
        rearrangement inequality the minimum pairs the highest frequencies with
        the shortest indices. Argued is not measured, so this attacks it: 400
        random permutations of the same distinct values, none of which may cost
        fewer bytes. A single win would refute the claim the encoder's verdict
        rests on - that a column losing under frequency ordering cannot be
        rescued by any other assignment.
        """
        rng = random.Random(245)
        # Shuffled, so the order the values first appear in is not their
        # frequency order. Built in descending frequency the two coincide, and
        # the attack below then compares frequency ordering against itself - a
        # test that cannot tell the encoder from a first-seen assignment.
        values = [f"value-{index}" for index in range(120) for _ in range(120 - index)]
        rng.shuffle(values)
        table = explorer.frequency_table(values)
        best = explorer.reference_bytes(values, table)

        beaten = []
        for _ in range(400):
            shuffled = list(table)
            rng.shuffle(shuffled)
            cost = explorer.reference_bytes(values, tuple(shuffled))
            if cost < best:
                beaten.append(cost)

        self.assertEqual(beaten, [])

    def test_equally_frequent_values_are_ordered_by_value_not_by_arrival(self):
        """Breaks if the frequency tiebreak drops its value half.

        A `Counter` iterates in first-seen order, so without the tiebreak two
        equally frequent values take their table positions from the order the
        graph happened to list its nodes in. Two builds of one graph would then
        emit different tables and different indices, and a committed page would
        differ between builds - hash randomisation has broken determinism here
        before and it is invisible until somebody diffs two pages.
        """
        forwards = explorer.frequency_table(["beta", "alpha", "beta", "alpha"])
        backwards = explorer.frequency_table(["alpha", "beta", "alpha", "beta"])

        self.assertEqual(forwards, backwards)
        self.assertEqual(forwards, ("alpha", "beta"))

    def test_a_column_of_mixed_shapes_is_declined_as_mixed_not_as_empty(self):
        """Breaks if a mixed column is encoded on a guess, or reported as empty.

        One comprehension builds every row today, so a mixed column cannot
        happen - and one that grew a second shape would otherwise be interned on
        a guess about which shape the decoder should expect.

        The second half is where the defect was. Nothing is costed, so every
        byte count is zero and the table is empty - and an empty table read as
        "holds nothing" made the report tell an operator that this populated
        column carried no information. Acting on that line deletes a column, and
        deleting a column renumbers every positional read in `app.js`. So this
        asserts the verdict is *mixed*, and that the line the operator actually
        sees neither calls the column empty nor quotes counts that are not the
        column's.
        """
        rows = rows_carrying(0, [f"LongLabel{index}" for index in range(4)])
        rows[0][0] = ["LongLabel0"]
        item = explorer.column_interning(rows, 0)
        line = explorer.column_line(item)

        self.assertTrue(item.mixed)
        self.assertFalse(item.interned)
        self.assertFalse(item.uninformative)
        self.assertIn("rows disagree about this column's shape", line)
        self.assertNotIn("carries no information", line)
        self.assertNotIn("distinct of", line)


class RoundTripTest(unittest.TestCase):
    """Encoding, and the refusal that stops a wrong one reaching a page."""

    def test_the_encoded_rows_decode_to_the_rows_they_replaced(self):
        """Breaks if a table is built in one order and read in another.

        Equality of whole rows, not of a count: the failure this catches
        substitutes one row's value for another's and leaves every length,
        every type and every row count exactly as it found them.
        """
        rows = sample_rows()
        _, tables = explorer.interning_plan(rows)
        encoded = explorer.encode_rows(rows, tables)

        self.assertNotEqual(encoded, rows)  # something was actually encoded
        self.assertEqual(explorer.decode_rows(encoded, tables), rows)

    def test_encoding_leaves_the_rows_it_was_given_alone(self):
        """Breaks if the rows are encoded in place.

        `entries` is read again after encoding - the recorded join counts its
        ticket column, and the join report counts the same one - so an encoded
        row would turn a count of rows carrying a ticket into a count of rows
        carrying an index. Both numbers would still look plausible.
        """
        rows = sample_rows()
        # The rows have to arrive unencoded for the snapshot below to mean
        # anything. They did not, once - a shared class attribute let an
        # alphabetically earlier test encode them in place first, and this test
        # then compared encoded rows against encoded rows and passed.
        self.assertIsInstance(rows[0][1], str)
        before = json.dumps(rows)
        _, tables = explorer.interning_plan(rows)
        explorer.encode_rows(rows, tables)

        self.assertEqual(json.dumps(rows), before)

    def test_an_encoding_that_does_not_round_trip_is_refused(self):
        """Breaks if the build stops verifying its own encoding.

        Every other gate over this page reads the encoded block, so a table in
        the wrong order is reported consistently by all of them: the page answers
        questions, reconciles its bytes and diffs clean between two builds, while
        showing one row's repository against another's file. Only reading both
        sides catches it, which is what the build does before it writes.
        """
        rows = sample_rows()
        _, tables = explorer.interning_plan(rows)
        encoded = explorer.encode_rows(rows, tables)
        column = sorted(tables)[0]
        tables[column][0], tables[column][1] = tables[column][1], tables[column][0]

        with self.assertRaises(ValueError) as raised:
            explorer.verify_round_trip(rows, encoded, tables)

        self.assertIn("does not decode back", str(raised.exception))

    def test_a_row_column_with_no_name_is_refused(self):
        """Breaks if a column added to the row can go unnamed.

        The report names every column it costed. A row one column wider than
        `COLUMN_NAMES` would shift every name onto its neighbour's column, and
        the report would then name one column while measuring another - the shape
        of every wrong measurement this pipeline has shipped.
        """
        widened = [row + ["extra"] for row in sample_rows()]

        with self.assertRaises(ValueError) as raised:
            explorer.interning_plan(widened)

        self.assertIn("COLUMN_NAMES", str(raised.exception))


class InterningReportTest(unittest.TestCase):
    """What the operator is told, which is the decision and not a total."""

    def test_every_column_is_named_with_the_verdict_and_its_counts(self):
        """Breaks if the report shrinks to a total, or names only the winners.

        A store told only that its page shrank cannot tell a column that declined
        because interning would have cost bytes from one that was never looked
        at, and which columns those are differs per estate. The counts have to be
        there too: the verdict is re-derivable from them, so a reader can check
        it rather than take it.
        """
        plan, _ = explorer.interning_plan(sample_rows())
        report = explorer.interning_report(plan)

        for item in plan:
            with self.subTest(column=item.name):
                line = next(text for text in report.splitlines() if f" {item.name} " in text + " ")
                self.assertIn("interned" if item.interned else "declined", line)
                self.assertIn(f"{item.distinct:,} distinct", line)
                self.assertIn(f"{item.occurrences:,} values", line)
        self.assertEqual(len(explorer.COLUMN_NAMES), len(plan))

    def test_a_column_empty_in_every_row_is_named_as_carrying_no_information(self):
        """Breaks if the step before the rule is dropped.

        No encoding of an empty column beats removing it, and interning one
        quietly represents nothing efficiently instead of saying so. Dropping a
        column renumbers every positional read in the page application, so it is
        a change to the row's shape rather than to its encoding - which is
        exactly why the operator has to be told rather than have it done.
        """
        plan, _ = explorer.interning_plan(sample_rows())
        deployment = next(item for item in plan if item.name == "deployment")

        self.assertTrue(deployment.uninformative)
        self.assertIn("carries no information", explorer.interning_report(plan))


class InternedPageTest(SettingsIsolated):
    """The encoding as it reaches a real page, through the real stage.

    The identities below are the reason to believe the costing at all. Every
    wrong measurement this pipeline has shipped was correct code answering a
    neighbouring question, and a cost model is unusually exposed to that: it
    predicts a byte count nobody checks. These check it against the bytes the
    page actually gained and lost.
    """

    LABEL_WIDTH = 40

    def _store(self, root: Path) -> None:
        """A store whose page has both a winning and a losing column.

        Sixty nodes over six repositories: the repository, community label and
        kind columns repeat heavily, and every label is distinct and long. So the
        page exercises both verdicts, and a build that interned everything or
        nothing would fail the assertions rather than pass them quietly.
        """
        config.configure(root=root)
        nodes = [
            {
                "id": f"n{index}",
                "label": f"ServiceComponentWithALongName{index:010d}"[: self.LABEL_WIDTH],
                "repo": f"repo-{index % 6}",
                "community": index % 4,
                "source_file": f"src/area{index % 6}/file{index}.ts",
                "file_type": "code",
                "metadata": {},
            }
            for index in range(60)
        ]
        links = [
            {"source": f"n{index}", "target": f"n{(index + step) % 60}"}
            for index in range(60)
            for step in (1, 2, 3)
        ]
        store_io.write_json(config.GRAPH_PATH, {"nodes": nodes, "links": links})
        store_io.write_json(config.LABELS_PATH, {str(c): f"Community area {c}" for c in range(4)})

    def _build(self) -> str:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(explorer.main(), 0)
        return out.getvalue()

    def _blocks(self) -> tuple[list, dict, str, str]:
        page = config.EXPLORER_PATH.read_text(encoding="utf-8")
        opening = '<script id="%s" type="application/json">'
        data = page.split(opening % "data")[1].split("</script>")[0]
        dicts = page.split(opening % "dicts")[1].split("</script>")[0]
        return json.loads(data), json.loads(dicts), data, dicts

    def test_the_page_decodes_to_exactly_the_rows_the_build_built(self):
        """Breaks if the page's rows stop being the rows the index computed.

        The round trip on a real page rather than on a hand-made list: the real
        graph, the real index, the real encoder, the block parsed back out of the
        file. Equality of the rows, because a count agrees with itself whatever
        the encoding did to the values inside them.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._store(Path(tmp).resolve())
            graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
            labels = json.loads(config.LABELS_PATH.read_text(encoding="utf-8"))
            with contextlib.redirect_stderr(io.StringIO()):
                entries, _ = explorer.build_index(graph, labels, {})
            self._build()
            rows, tables, _, _ = self._blocks()

        self.assertTrue(tables, "no column was interned, so this asserts nothing")
        self.assertEqual(explorer.decode_rows(rows, tables), entries)

    def test_the_table_costs_exactly_what_the_page_writes(self):
        """Breaks if the separator convention drifts from the serialisation.

        The bug this caught while it was being written: `json.dumps` writes
        `", "` and `": "`, two bytes each and not one, so a costing that assumed
        one understated every table by its cardinality. Understating the table
        overstates the saving, which predicts wins that are losses - the one
        direction of error that matters here. The convention is not transferable
        between encodings, so it is pinned against this encoding's own output.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._store(Path(tmp).resolve())
            self._build()
            rows, tables, _, dicts = self._blocks()

        plan, _ = explorer.interning_plan(explorer.decode_rows(rows, tables))
        modelled = sum(item.table_bytes for item in plan if item.interned)
        self.assertEqual(modelled, len(dicts.encode("utf-8")))

    def test_the_modelled_saving_is_the_bytes_the_page_actually_lost(self):
        """Breaks if the cost model computes a neighbour of the saving.

        The claim is "this many bytes came off the page", so it is checked
        against the page: the plain block, less the encoded block, less the
        dictionaries the encoding added. A model that mis-costs references, or
        forgets that the dictionaries are themselves page bytes, still adds up
        internally and disagrees here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._store(Path(tmp).resolve())
            self._build()
            rows, tables, data, dicts = self._blocks()

        decoded = explorer.decode_rows(rows, tables)
        plan, _ = explorer.interning_plan(decoded)
        plain = json.dumps(decoded, ensure_ascii=False).replace("</", "<\\/")
        actual = len(plain.encode("utf-8")) - len(data.encode("utf-8")) - len(dicts.encode("utf-8"))

        self.assertEqual(sum(item.saving for item in plan if item.interned), actual)
        self.assertGreater(actual, 0)

    def test_a_build_whose_encoding_is_wrong_stops_instead_of_writing_a_page(self):
        """Breaks if the build stops verifying its encoding before it writes.

        The checker is covered directly elsewhere; this covers the wiring, which
        is the half that can be removed without any test noticing. Broken at the
        seam rather than by editing a table, so what fails is the build rather
        than a hand-made object. Nothing may be written: a page assembled from a
        wrong encoding answers questions, reconciles its bytes and diffs clean
        between two builds, all while showing one row's values against another's.
        """
        real = explorer.encode_rows
        self.addCleanup(setattr, explorer, "encode_rows", real)
        explorer.encode_rows = lambda entries, tables: real(entries, tables)[::-1]

        with tempfile.TemporaryDirectory() as tmp:
            self._store(Path(tmp).resolve())
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(ValueError) as raised,
            ):
                explorer.main()
            written = config.EXPLORER_PATH.exists()

        self.assertIn("does not decode back", str(raised.exception))
        self.assertFalse(written)

    def test_the_decision_is_reported_per_column_on_a_real_build(self):
        """Breaks if the report stops reaching the operator running the build.

        A saving printed nowhere is one nobody can act on, and this page is built
        by a store operator at a terminal. Both verdicts have to appear: a report
        that only listed winners would leave a declined column looking like a
        column nobody costed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._store(Path(tmp).resolve())
            stdout = self._build()

        self.assertIn("Data-block interning", stdout)
        self.assertRegex(stdout, r"\n  interned  repo ")
        self.assertRegex(stdout, r"\n  declined  label ")

    def test_two_builds_of_one_graph_write_the_same_page(self):
        """Breaks if any part of the encoding follows an unordered iteration.

        Deterministic output is a stated feature of every artefact here. Within
        one process this only catches an ordering that varies run to run; the
        cross-process case, where `PYTHONHASHSEED` differs, is
        `test_two_builds_are_identical.py` over the real fixture page.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._store(Path(tmp).resolve())
            self._build()
            first = config.EXPLORER_PATH.read_bytes()
            self._build()
            second = config.EXPLORER_PATH.read_bytes()

        self.assertEqual(first, second)


class ReportedNumbersTest(unittest.TestCase):
    """The report's own arithmetic, which an operator is invited to check."""

    def test_the_percentage_is_quoted_against_value_bytes_not_the_block(self):
        """Breaks if the share is quoted against the whole data block.

        Brackets, commas and row scaffolding are a floor no encoding touches, and
        their share of the block differs measurably between estates - so a
        percentage of the whole block would flatter one store and understate
        another for reasons that have nothing to do with the encoding.
        """
        plan, _ = explorer.interning_plan(sample_rows())
        report = explorer.interning_report(plan)

        value_bytes = sum(item.field_bytes for item in plan)
        saved = sum(item.saving for item in plan if item.interned)
        self.assertIn(f"{value_bytes:,} value bytes", report)
        self.assertIn(f"({saved * 100 // value_bytes}% of them)", report)
        self.assertIn("structural bytes", report)

    def test_the_net_line_is_the_sum_of_the_columns_that_won(self):
        """Breaks if the headline stops being the sum of the reported verdicts.

        A total nobody can reach from the lines above it is a second expression
        for the same quantity, which is how two numbers describing one thing
        start disagreeing.
        """
        plan, _ = explorer.interning_plan(sample_rows())
        report = explorer.interning_report(plan)

        saved = sum(item.saving for item in plan if item.interned)
        net = re.search(r"net ([\d,]+) bytes", report)
        assert net is not None
        self.assertEqual(int(net.group(1).replace(",", "")), saved)


if __name__ == "__main__":
    unittest.main()
