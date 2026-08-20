"""Every derived number in the load-cost tables must follow from the raw ones beside it.

This exists because of an error it would have caught. §10 published a per-object
sequence of `2,396 / 643 / 1,493` and concluded from its shape that cost per object
does not track edge density. The middle figure was bytes-per-object **on disk**;
the other two were bytes-per-object **in memory**. Mixing the two produced a
non-monotonic series, and the non-monotonicity was then reported as a finding - so
a real relationship was published as its own absence.

The tables state their own raw inputs - bytes on disk, node count, edge count, peak
RSS - and their own derived columns. That makes them checkable against themselves
with no fixture and no re-measurement: `643` cannot be reconciled with the 3.75 GB
and 2,280,711 objects on its own row, which imply 1,765. It is off by 2.7x.

**What this does not check** is whether the measurements are right. Two of the three
graphs are on another operator's machine and cannot be re-measured here. It checks
that the arithmetic the prose relies on is the arithmetic the numbers support, which
is the half that was wrong.

Tolerances come from the precision each value is quoted to rather than a flat
percentage: `0.10 GB` means [0.095, 0.105), a 5% band, while `1.36 GB` means 0.4%.
A single tolerance would either reject the smallest row or accept anything.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "building-a-knowledge-store.md"
GB = 1024**3

# A markdown table row: leading pipe, cells, trailing pipe. Separator rows excluded
# by requiring at least one digit somewhere.
ROW = re.compile(r"^\|(?P<cells>.+)\|\s*$")


def cells(line: str) -> list[str] | None:
    match = ROW.match(line.strip())
    if not match:
        return None
    parts = [c.strip().strip("*").replace("`", "") for c in match.group("cells").split("|")]
    return parts if any(any(ch.isdigit() for ch in p) for p in parts) else None


def number(text: str) -> float:
    """A quoted figure, without its unit or thousands separators."""
    return float(re.sub(r"[^0-9.]", "", text))


def band(text: str) -> float:
    """Half the last decimal place quoted - the interval the figure stands for.

    `0.10` stands for anything in [0.095, 0.105), so deriving from it carries 5%
    of slack; `1.36` carries 0.4%. Using one tolerance for both would either fail
    the smallest row or pass anything.
    """
    digits = re.sub(r"[^0-9.]", "", text)
    if "." not in digits:
        return 0.5
    return 0.5 * 10 ** -len(digits.split(".")[1])


def guide_rows() -> tuple[list[list[str]], list[list[str]]]:
    """(raw table rows, derived table rows) from section 10."""
    text = GUIDE.read_text(encoding="utf-8")
    section = text[text.index("## 10.") :]
    raw, derived = [], []
    for line in section.splitlines():
        parsed = cells(line)
        if not parsed:
            continue
        if len(parsed) == 7:
            raw.append(parsed)
        elif len(parsed) == 4:
            derived.append(parsed)
    return raw, derived


class TheLoadCostTablesAgreeWithThemselves(unittest.TestCase):
    def setUp(self) -> None:
        self.raw, self.derived = guide_rows()

    def test_the_tables_were_actually_found(self):
        """A parser that silently matched nothing would pass every check below.

        The section has been renumbered once already and its headings reworded more
        than once, so this is the guard that keeps a layout change from turning this
        file into a green check of an empty set.
        """
        self.assertEqual(len(self.raw), 3, f"raw rows found: {self.raw}")
        self.assertEqual(len(self.derived), 3, f"derived rows found: {self.derived}")
        self.assertEqual([r[0] for r in self.raw], [d[0] for d in self.derived])

    def test_edges_per_node_follows_from_the_counts(self):
        for name, _disk, nodes, edges, quoted, _rss, _factor in self.raw:
            with self.subTest(graph=name):
                self.assertAlmostEqual(
                    number(edges) / number(nodes), number(quoted), delta=band(quoted)
                )

    def test_the_amplification_factor_follows_from_rss_and_disk(self):
        for name, disk, _nodes, _edges, _epn, rss, factor in self.raw:
            with self.subTest(graph=name):
                implied = number(rss) / number(disk)
                # Both inputs are quoted, so the slack is their bands combined.
                slack = number(factor) * (band(disk) / number(disk) + band(rss) / number(rss))
                self.assertAlmostEqual(implied, number(factor), delta=slack + band(factor))

    def test_rss_per_node_and_per_object_come_from_memory_not_disk(self):
        """The error this file exists for.

        `643` for the middle row is bytes-per-object on DISK. Its own row states
        3.75 GB of RSS over 2,280,711 objects, which is 1,765 - so the published
        figure was 2.7x out and the sequence it sat in was reported as evidence.
        """
        by_name = {row[0]: row for row in self.raw}
        for name, _epn, per_node, per_object in self.derived:
            with self.subTest(graph=name):
                _, disk, nodes, edges, _e, rss, _f = by_name[name]
                rss_bytes = number(rss) * GB
                slack = band(rss) / number(rss)
                self.assertAlmostEqual(
                    rss_bytes / number(nodes),
                    number(per_node),
                    delta=number(per_node) * slack + band(per_node),
                    msg="RSS per node does not follow from this row's peak RSS",
                )
                self.assertAlmostEqual(
                    rss_bytes / (number(nodes) + number(edges)),
                    number(per_object),
                    delta=number(per_object) * slack + band(per_object),
                    msg="RSS per object does not follow from this row's peak RSS",
                )
                # And explicitly NOT the disk-derived value, which is what was published.
                disk_per_object = number(disk) * GB / (number(nodes) + number(edges))
                if abs(disk_per_object - number(per_object)) < 1:
                    self.fail(
                        f"{name}: per-object figure {per_object} matches the DISK-derived "
                        "value, which is the error this test exists for"
                    )


class ThisSuiteCanStillTell(unittest.TestCase):
    """Publish the original error into a copy of the table and require a failure.

    The checks above compare a document against itself, so they would also pass if
    the comparison had quietly stopped discriminating - and the whole reason this
    file exists is that a self-consistent-looking table was not.
    """

    ROWS = [["committed graph.json", "1.36 GB", "785,493", "1,495,218", "1.90", "3.75 GB", "2.76x"]]

    def test_the_disk_derived_per_object_value_is_rejected(self):
        """643 is what was published; 1,765 is what the row implies.

        The published 643 was computed from the exact byte count, and the table quotes
        `1.36 GB`, so reproducing it from the table lands at 640 - within the rounding
        band and outside a delta of 1, which is what my first version of this
        assertion used. The band is the same one the checks above derive.
        """
        _, disk, nodes, edges, _e, rss, _f = self.ROWS[0]
        objects = number(nodes) + number(edges)
        implied = number(rss) * GB / objects
        published = number(disk) * GB / objects
        self.assertAlmostEqual(
            published,
            643,
            delta=643 * (band(disk) / number(disk)) + 1,
            msg="the disk-derived figure, within what 1.36 GB stands for",
        )
        self.assertAlmostEqual(implied, 1765, delta=1765 * (band(rss) / number(rss)) + 1)
        self.assertGreater(
            abs(implied - published) / implied, 0.5, "the two must be far apart to be caught"
        )

    def test_a_correct_value_is_accepted(self):
        """Without this the test above would pass against a checker that rejects
        everything, which is the same vacuity one level up."""
        _, _disk, nodes, edges, _e, rss, _f = self.ROWS[0]
        implied = number(rss) * GB / (number(nodes) + number(edges))
        self.assertAlmostEqual(implied, 1765, delta=implied * (band(rss) / number(rss)) + 1)


if __name__ == "__main__":
    unittest.main()
