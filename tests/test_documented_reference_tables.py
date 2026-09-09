"""The two reference tables, and three enumerations, held against the code.

Every claim here was wrong in the tree when this file was written, and each was
wrong in the same way: a table or a count that was right when it was typed and
that nothing could notice going stale.

- The language-support table said Terraform gets **no AST support** while
  `status.OPTIONAL_EXTRACTORS` shipped an HCL extractor and a line telling the
  operator how to install it, and SQL - the other entry in that tuple - was
  absent from the table entirely. A reader planning an estate by that table
  budgets nothing for its infrastructure repositories and gets a graph short by
  whatever the extras would have contributed.
- The retrieval layer table attributed `knowledge/intent/*.json.gz` to `intent`,
  which is a glob over four files, two of them written by other stages; had no
  row for deep dives at all; and named one of the three stages that add nodes to
  the graph.
- `how-it-works.md` said `_remap_refusal` has three refusals and then listed the
  wrong three, counting the exact-set withdrawal - which is the carry criterion,
  not a refusal - and omitting the snapshot-shares-no-node-ids one, so the
  refusal an operator meets after pointing `remap` at the wrong snapshot appeared
  in no document.
- `grounding-and-verification.md` taught a two-group authored/carried split of
  the grounding flag rate. `_provenance_groups` has four, and the split into four
  is the correction: prose carried by a remap that did not move its community
  grounds as authored prose does, and reporting it beside the carried-across-a-
  move figure invites the opposite conclusion.
- `building-a-knowledge-store.md` gave a two-way reading of the withdrawal
  reasons - collisions against drops below the bar - under a default criterion
  that cannot produce a drop below the bar at all.

What each check reads is stated with it, because a gate over prose can go
vacuous by improvement: a table that moves, a heading that is reworded, a
paragraph that is split. Every extractor loop and every table parse asserts it
found something, and `TheseChecksCanStillTell` drives each checker with the
defect it was written for and with a correct input, so a run says whether the
checks can still discriminate rather than only whether they passed.

Expected values are derived from the code - `OPTIONAL_EXTRACTORS`, the config
paths, `cli.STAGES`, the AST of `_remap_refusal`, the keys `_provenance_groups`
returns - never by re-reading the prose with the parser that produced it.

What is not pinned here, said rather than shipped weakly: the *identity* of the
three refusals. The count comes from the code and the two flags its messages
name are required in the prose, but the third refusal names no flag, so a
document could still count three and describe the wrong third. That is the
defect above, and only a reader catches it today.
"""

from __future__ import annotations

import ast
import re
import unittest
from fnmatch import fnmatch
from pathlib import Path

from knowledgestore import build_community_summaries as summaries
from knowledgestore import cli, config, status

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "knowledgestore"
GUIDE = ROOT / "docs" / "building-a-knowledge-store.md"
RETRIEVAL = ROOT / "docs" / "retrieval-architecture.md"
HOW_IT_WORKS = ROOT / "docs" / "how-it-works.md"
GROUNDING = ROOT / "docs" / "grounding-and-verification.md"

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
BACKTICKED = re.compile(r"`([^`]+)`")


def table_rows(text: str, heading: str) -> list[list[str]]:
    """The rows of the first markdown table under `heading`, cells stripped.

    Separator rows and the header row are dropped. Stops at the next heading, so
    a second table in the same section cannot be read as more rows of the first.
    """
    start = text.index(heading)
    section = text[start + len(heading) :]
    end = section.find("\n## ")
    if end != -1:
        section = section[:end]
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line[1:-1].split("|")]
        if all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append(cells)
    return rows[1:] if rows else rows


def config_paths() -> dict[str, set[str]]:
    """Every store-relative path `config` derives, mapped to the names holding it.

    The names are what the produced-by check needs: a stage that writes an
    artefact refers to it as `config.<NAME>`, so the table's third column can be
    held against the module of the stage it names.
    """
    found: dict[str, set[str]] = {}
    for name, value in vars(config).items():
        if name.startswith("_") or not isinstance(value, Path):
            continue
        try:
            relative = value.relative_to(config.ROOT)
        except ValueError:
            continue
        found.setdefault(str(relative), set()).add(name)
    return found


def resolve(token: str, paths: dict[str, set[str]]) -> set[str]:
    """The config names a table's artefact token stands for, or an empty set.

    A `*` token is matched against the paths themselves and against their parent
    directory, because a documented `docs/topics/*.md` is `TOPICS_DOCS_DIR` and a
    documented `knowledge/intent/*.json.gz` is the files inside it.
    """
    if "*" not in token:
        return set(paths.get(token, set()))
    names = {name for path, held in paths.items() if fnmatch(path, token) for name in held}
    return names or set(paths.get(token.rsplit("/", 1)[0], set()))


def language_table_problems(rows: list[list[str]]) -> list[str]:
    """Where the language-support table disagrees with `OPTIONAL_EXTRACTORS`.

    The install command is what is required of the prose, not a form of words:
    `status` tells an operator to run `pip install 'graphifyy[<extra>]'`, so a row
    naming that string is a row that cannot also be denying the extractor exists.
    An extra with no such row is the Terraform defect; a row that names the
    install and denies AST support in the same breath is that defect half-fixed.
    """
    problems = []
    for suffixes, _module, extra in status.OPTIONAL_EXTRACTORS:
        install = f"graphifyy[{extra}]"
        named = [row for row in rows if install in " | ".join(row)]
        if not named:
            problems.append(
                f"no row names `pip install '{install}'`, so the table does not say that "
                f"{', '.join('.' + s for s in suffixes)} reach the graph once it is installed"
            )
            continue
        for row in named:
            if "no ast support" in " | ".join(row).lower():
                problems.append(f"the row naming {install} also denies AST support: {row[0]}")
    return problems


def layer_table_problems(rows: list[list[str]], paths: dict[str, set[str]]) -> list[str]:
    """Where the retrieval layer table disagrees with the config paths and the stages.

    Three disagreements, and the middle one is the mis-attribution this file was
    written for: an artefact the table names that `config` does not derive; a
    stage named beside an artefact its module never refers to; and a layer the
    page embeds that the table does not place at all.
    """
    problems = []
    covered: set[str] = set()
    for row in rows:
        layer, artefacts, producer = row[0], row[1], row[2]
        names: set[str] = set()
        for token in BACKTICKED.findall(artefacts):
            resolved = resolve(token, paths)
            if not resolved:
                problems.append(f"{layer}: `{token}` is not a path config derives")
            names |= resolved
            covered |= resolved
        for token in BACKTICKED.findall(producer):
            if token not in cli.STAGES:
                problems.append(f"{layer}: `{token}` is not a stage of this release")
                continue
            module = SOURCE / f"{cli.STAGES[token][0]}.py"
            source = module.read_text(encoding="utf-8")
            for name in sorted(names):
                if f"config.{name}" not in source:
                    problems.append(
                        f"{layer}: `{token}` is credited with {name}, which "
                        f"{module.name} never refers to"
                    )
    for embedded in status.EMBEDDED_LAYERS:
        held = set(paths.get(embedded, set()))
        if held and not held & covered:
            problems.append(f"{embedded} is embedded in the page and the table does not place it")
    return problems


def refusal_returns() -> list[str]:
    """The message of each refusal `_remap_refusal` can return, from its AST.

    Read from the source rather than by calling it: the point is how many
    refusals exist, and a call exercises one path. A bare `return None` is the
    permission to proceed and is not one of them.
    """
    tree = ast.parse((SOURCE / "build_community_summaries.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_remap_refusal"
    )
    messages = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if isinstance(node.value, ast.Constant) and node.value.value is None:
            continue
        messages.append(
            "".join(
                part.value
                for part in ast.walk(node.value)
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        )
    return messages


def remap_bullet() -> str:
    """The bullet in `how-it-works.md` that describes `remap`, alone.

    Bounded at the next bullet so a count from the `verify` bullet below it
    cannot satisfy a check about this one.
    """
    text = HOW_IT_WORKS.read_text(encoding="utf-8")
    start = text.index("- **`remap` protects continuity")
    rest = text[start + 1 :]
    end = rest.find("\n- **")
    return rest if end == -1 else rest[:end]


def paragraphs(path: Path) -> list[str]:
    return [block for block in path.read_text(encoding="utf-8").split("\n\n") if block.strip()]


def stated_refusal_count(bullet: str) -> int | None:
    """The number of refusals the prose claims, or None if it claims none."""
    stated = re.search(r"\b(\w+) refusals\b", bullet)
    return NUMBER_WORDS.get(stated.group(1).lower()) if stated else None


def paragraphs_naming(blocks: list[str], states: list[str]) -> list[str]:
    """The blocks naming every state, which must be exactly one.

    One paragraph rather than anywhere in the document: `authored` is a common
    word in a document about authored prose, so a check satisfied by the whole
    text would pass over the two-group framing this replaced.
    """
    return [block for block in blocks if all(state in block for state in states)]


class TheLanguageSupportTableAgreesWithTheExtractors(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = table_rows(GUIDE.read_text(encoding="utf-8"), "## 3. What extraction actually")

    def test_the_table_was_actually_found(self):
        """The section has been renumbered before, and a parse that matched
        nothing would pass the check below over an empty list."""
        self.assertGreater(len(self.rows), 5, f"rows found: {self.rows}")
        self.assertTrue(
            any("Java" in row[0] for row in self.rows), f"not the content table: {self.rows}"
        )

    def test_every_optional_extractor_has_an_actionable_row(self):
        self.assertGreater(len(status.OPTIONAL_EXTRACTORS), 0, "nothing to check against")
        self.assertEqual([], language_table_problems(self.rows))


class TheRetrievalLayerTableAgreesWithTheConfigPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = table_rows(RETRIEVAL.read_text(encoding="utf-8"), "## Where each answer layer")
        self.paths = config_paths()

    def test_the_table_and_the_paths_were_actually_found(self):
        self.assertGreater(len(self.rows), 5, f"rows found: {self.rows}")
        self.assertIn(str(config.SUMMARIES_PATH.relative_to(config.ROOT)), self.paths)

    def test_each_artefact_is_a_config_path_produced_by_the_stage_named(self):
        self.assertEqual([], layer_table_problems(self.rows, self.paths))


class TheDocumentedEnumerationsAgreeWithTheCode(unittest.TestCase):
    def test_the_refusal_count_is_the_number_of_refusals(self):
        """The count, from the AST, against the number word in the prose.

        The count rather than the wording, so the bullet stays free to be
        rewritten and a fourth refusal fails it.
        """
        messages = refusal_returns()
        self.assertEqual(3, len(messages), "the refusals themselves changed, so the prose must")
        self.assertEqual(len(messages), stated_refusal_count(remap_bullet()))

    def test_every_flag_a_refusal_names_is_documented_with_it(self):
        bullet = remap_bullet()
        flags = {flag for message in refusal_returns() for flag in re.findall(r"--[a-z]+", message)}
        self.assertEqual({"--floor", "--coverage"}, flags, "the refusals' flags changed")
        for flag in sorted(flags):
            with self.subTest(flag=flag):
                self.assertIn(flag, bullet)
        self.assertIn(str(summaries.DEFAULT_FLOOR), bullet)

    def test_the_provenance_split_is_documented_in_its_own_states(self):
        """Every group `_provenance_groups` returns, named in one paragraph."""
        groups = list(summaries._provenance_groups({}, []))
        self.assertEqual(4, len(groups), "the states changed, so the document must")
        found = paragraphs_naming(paragraphs(GROUNDING), groups)
        self.assertEqual(1, len(found), f"paragraphs naming all of {sorted(groups)}: {len(found)}")

    def test_below_the_bar_is_unreachable_under_the_shipped_criterion(self):
        """The behaviour the guide's reasons paragraph rests on.

        Five members, two of them landing together in the largest new community:
        recall 0.4, under the 0.6 bar, and not set equality either. Under the
        shipped `exact` criterion the withdrawal reads `not-identical`; only
        `--carry overlap` can produce `below-bar`. A guide that offers a reader
        below-bar against collisions is offering a count that cannot move.
        """
        self.assertEqual(summaries.CARRY_EXACT, summaries.DEFAULT_CARRY)
        members = ["a", "b", "c", "d", "e"]
        new_community = {"a": "10", "b": "10", "c": "11", "d": "12", "e": "13"}
        expectations = (
            (summaries.DEFAULT_CARRY, "not-identical"),
            (summaries.CARRY_OVERLAP, "below-bar"),
        )
        for carry, expected in expectations:
            with self.subTest(carry=carry):
                claims, displaced = summaries._claim_targets(
                    {"1": "prose"},
                    {"1": members},
                    new_community,
                    summaries.DEFAULT_BAR,
                    summaries.DEFAULT_PRECISION,
                    carry,
                )
                self.assertEqual({}, claims)
                self.assertEqual(expected, displaced["1"]["reason"])
                self.assertEqual(0.4, displaced["1"]["share"])

    def test_the_guide_quotes_the_thresholds_the_code_sets(self):
        """The two percentages in the reasons paragraph, from the defaults."""
        blocks = [block for block in paragraphs(GUIDE) if "`--carry overlap`" in block]
        self.assertEqual(1, len(blocks), "the reasons paragraph was not found")
        self.assertIn(f"{int(summaries.DEFAULT_BAR * 100)}%", blocks[0])
        self.assertIn(f"{int(summaries.DEFAULT_PRECISION * 100)}%", blocks[0])

    def test_every_label_the_estate_pass_prints_is_documented(self):
        """All three labels, read from the function that prints them.

        The document quoted `[not in graph]` alone, which is the label a store
        with no history datasets gets - so a reader with history datasets was
        told to look for a string their run never prints.
        """
        tree = ast.parse((SOURCE / "build_community_summaries.py").read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_report_absent_terms"
        )
        labels = set(re.findall(r"\[not in [^\]]+\]", ast.unparse(function)))
        self.assertEqual(3, len(labels), f"labels found: {sorted(labels)}")
        text = GROUNDING.read_text(encoding="utf-8")
        for label in sorted(labels):
            with self.subTest(label=label):
                self.assertIn(label, text)


class TheseChecksCanStillTell(unittest.TestCase):
    """Each checker driven with the defect it was written for, and with a
    correct input beside it.

    The checks above compare documents against the code, so they would also pass
    if a parse had quietly stopped matching - and both tables were parseable and
    wrong for months.
    """

    HEADER = ["Content", "What you get", "Notes"]
    GOOD_LANGUAGE = [
        HEADER,
        ["Terraform / HCL", "A node per block, with `pip install 'graphifyy[terraform]'`", ""],
        ["SQL", "A node per table and view, with `pip install 'graphifyy[sql]'`", ""],
    ]

    def test_a_table_denying_an_installed_extractor_is_rejected(self):
        rows = [self.GOOD_LANGUAGE[0], ["Terraform / HCL", "**Nothing.** No AST support", ""]]
        problems = language_table_problems(rows)
        self.assertTrue(any("graphifyy[terraform]" in p for p in problems), problems)
        self.assertTrue(any("graphifyy[sql]" in p for p in problems), problems)

    def test_a_row_that_names_the_install_and_denies_support_is_rejected(self):
        rows = [
            self.GOOD_LANGUAGE[0],
            self.GOOD_LANGUAGE[2],
            ["Terraform / HCL", "No AST support, `pip install 'graphifyy[terraform]'`", ""],
        ]
        self.assertTrue(
            any("denies AST support" in p for p in language_table_problems(rows)),
            "a self-contradicting row passed",
        )

    def test_the_language_checker_accepts_a_correct_table(self):
        """Without this, the two above would pass against a checker that
        rejected everything."""
        self.assertEqual([], language_table_problems(self.GOOD_LANGUAGE))

    def test_the_intent_glob_attributed_to_one_stage_is_rejected(self):
        """The published row: a glob over four files credited to `intent`."""
        rows = [
            ["Layer", "Artefact", "Produced by"],
            ["Intent index", "`knowledge/intent/*.json.gz`", "`intent`"],
        ]
        problems = layer_table_problems(rows, config_paths())
        self.assertTrue(any("TICKET_TRACKER_PATH" in p for p in problems), problems)
        self.assertTrue(any("build_intent_index.py" in p for p in problems), problems)

    def test_an_omitted_embedded_layer_is_rejected(self):
        """The deep-dives row, absent - the omission a per-row check cannot see."""
        rows = [
            ["Layer", "Artefact", "Produced by"],
            ["Community summaries", "`knowledge/summaries/communities.json`", "`summaries`"],
        ]
        problems = layer_table_problems(rows, config_paths())
        self.assertTrue(any("deep-dives/dives.json" in p for p in problems), problems)

    def test_an_artefact_config_does_not_derive_is_rejected(self):
        rows = [["Layer", "Artefact", "Produced by"], ["Briefs", "`briefs.json`", "`topics`"]]
        self.assertTrue(
            any(
                "not a path config derives" in p for p in layer_table_problems(rows, config_paths())
            )
        )

    def test_the_layer_checker_accepts_the_shipped_table(self):
        """The same input the real check reads, asserted to produce nothing, so a
        rejection above is discrimination rather than a checker that fails on
        everything."""
        rows = table_rows(RETRIEVAL.read_text(encoding="utf-8"), "## Where each answer layer")
        self.assertEqual([], layer_table_problems(rows, config_paths()))

    def test_a_miscounted_refusal_sentence_is_rejected(self):
        """The prose side of the count, driven with a wrong one and a right one."""
        wrong = next(
            word for word, value in NUMBER_WORDS.items() if value != len(refusal_returns())
        )
        right = next(
            word for word, value in NUMBER_WORDS.items() if value == len(refusal_returns())
        )
        self.assertNotEqual(len(refusal_returns()), stated_refusal_count(f"with {wrong} refusals:"))
        self.assertEqual(len(refusal_returns()), stated_refusal_count(f"with {right} refusals:"))
        self.assertIsNone(stated_refusal_count("protects continuity, and refuses to run when"))

    def test_a_two_group_provenance_paragraph_is_rejected(self):
        """The framing this replaced, and the one that replaced it.

        The old paragraph named authored prose and carried prose and nothing
        else, so no paragraph named all four states - which is the reading a
        reader took from it.
        """
        groups = list(summaries._provenance_groups({}, []))
        old = (
            "Tuned against authored summaries: summaries written directly against their own "
            "digest flagged at 9%, while summaries carried across a re-cluster flagged at 37%."
        )
        self.assertEqual([], paragraphs_naming([old], groups))
        self.assertEqual(1, len(paragraphs_naming(paragraphs(GROUNDING), groups)))


if __name__ == "__main__":
    unittest.main()
