"""Every explorer row can open its file on the code host, pinned to the build commit.

The page does not carry a URL per row: every URL is unique, so that column could
not be interned, and on real stores it would have grown the page by about a third.
It carries one base per repository in #config, the line anchor per row, and the
names of the repositories built by the single-root route; `app.js` joins them.

What these tests hold is the contract between the two halves: the parts the page
carries, joined the way the page joins them, are the graph's `url` for the same
node - as the real `source-urls` stage writes it - byte for byte. The JavaScript
join is held to the same standard against a page built by the real pipeline, in
`tests/explorer/page-regression.mjs`.

Every test builds the real page through `build_explorer.main()` over a real store
root, and each names the defect it catches.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from pathlib import Path
from urllib.parse import quote

from settings_isolation import SettingsIsolated
from knowledgestore import build_explorer as explorer
from knowledgestore import build_source_urls, config, source_urls
from knowledgestore import io as store_io

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"

REPOSITORIES_TXT = """\
# Format: name|clone-url|default-branch
example-repo|git@github.com:example-org/example-repo.git|main
single-repo|https://github.com/example-org/single-repo.git|main
elsewhere-repo|git@gitlab.example.com:example-org/elsewhere-repo.git|main
unsynced-repo|git@github.com:example-org/unsynced-repo.git|main
absent-repo|git@github.com:example-org/absent-repo.git|main
"""

PROVENANCE = {
    "repositories": {
        "example-repo": {"sha": SHA, "branch": "main", "committed": "2024-01-01T00:00:00+00:00"},
        "single-repo": {
            "sha": OTHER_SHA,
            "branch": "main",
            "committed": "2024-01-01T00:00:00+00:00",
        },
        "elsewhere-repo": {"sha": SHA, "branch": "main", "committed": "2024-01-01T00:00:00+00:00"},
        "absent-repo": {"sha": SHA, "branch": "main", "committed": "2024-01-01T00:00:00+00:00"},
    }
}


def _node(node_id: str, repo: str, path: str, location: str | None = None) -> dict:
    node = {
        "id": node_id,
        "label": f"Component{node_id.title()}",
        "repo": repo,
        "source_file": path,
        "community": 1,
        "file_type": "code",
        "metadata": {},
    }
    if location is not None:
        node["source_location"] = location
    return node


# Every node carries a line, so a row left without one was left without it by the
# page rather than by the graph.
NODES = [
    _node("a", "example-repo", "src/a.ts", "L10"),
    _node("b", "example-repo", "src/odd name #2 (draft).ts", "L3-L7"),
    # Inside no repository: the graph refuses it, so the page must too.
    _node("c", "example-repo", "../escape.ts", "L9"),
    # The single-root route: every file of this repository carries its prefix.
    _node("d", "single-repo", "repositories/single-repo/src/x.ts", "L4"),
    _node("e", "single-repo", "repositories/single-repo/lib/café.ts", "L1"),
    # Hosted somewhere other than github.com.
    _node("f", "elsewhere-repo", "src/f.ts", "L2"),
    # Configured, but no recorded commit.
    _node("g", "unsynced-repo", "src/g.ts", "L5"),
]
# A ring, so every node clears the entry gate whatever it is set to.
LINKS = [
    {"source": NODES[i]["id"], "target": NODES[(i + 1) % len(NODES)]["id"]}
    for i in range(len(NODES))
]


class SourceLinksOnThePage(SettingsIsolated):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Resolved: `configure(root=...)` resolves what it is given, and the
        # embedded-layer paths must be relative to it.
        self.root = Path(self._tmp.name).resolve()
        config.configure(root=self.root, MIN_ENTRY_DEGREE=0)
        store_io.write_json(config.GRAPH_PATH, {"nodes": NODES, "links": LINKS})
        store_io.write_json(config.LABELS_PATH, {})
        config.REPOSITORIES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        config.REPOSITORIES_CONFIG.write_text(REPOSITORIES_TXT, encoding="utf-8")
        store_io.write_json(config.PROVENANCE_PATH, PROVENANCE)

    def _build(self) -> tuple[dict, list, str]:
        """`(the page's #config, its decoded rows, what the stage printed to stderr)`."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(explorer.main(), 0)
        self.stdout = out.getvalue()
        page = config.EXPLORER_PATH.read_text(encoding="utf-8")

        def block(name: str):
            opening = f'<script id="{name}" type="application/json">'
            return json.loads(page.split(opening)[1].split("</script>")[0])

        page_config = block("config")
        rows = explorer.decode_rows(
            block("data"), block("dicts"), page_config.get(explorer.DROPPED_COLUMNS_KEY, {})
        )
        return page_config, rows, err.getvalue()

    def _graph_urls(self) -> dict[tuple[str, str], str | None]:
        """The `url` the real `source-urls` stage gives each node, by (repo, file)."""
        graph = {"nodes": [dict(node) for node in NODES], "links": LINKS}
        build_source_urls.annotate(graph, source_urls.SourceUrls.from_store())
        return {(node["repo"], node["source_file"]): node.get("url") for node in graph["nodes"]}

    def test_the_page_carries_a_base_for_each_repository_its_rows_can_link(self):
        """Breaks if a repository the graph links nothing in is given a base - one on
        another host, or with no recorded commit - so its rows link a file that is
        not there; if the single-root repositories are not named, so their rows keep
        the corpus prefix; or if the base is built other than as the graph's is.

        `absent-repo` is configured and synced but has no row, so it is not carried:
        the bases cost a line per repository on the page, not per configured one.
        """
        page_config, _, _ = self._build()

        self.assertEqual(
            page_config[explorer.SOURCE_BASES_KEY],
            {
                "example-repo": f"https://github.com/example-org/example-repo/blob/{SHA}/",
                "single-repo": f"https://github.com/example-org/single-repo/blob/{OTHER_SHA}/",
            },
        )
        self.assertEqual(page_config[explorer.SOURCE_SINGLE_ROOT_KEY], ["single-repo"])

    def test_each_row_joined_to_its_base_is_the_graph_url_for_its_node(self):
        """Breaks if the parts the page carries stop composing to the graph's link:
        the line anchor dropped or read differently, the single-root route decided
        differently, or a row given parts for a link the graph refuses.

        The join here is the one `app.js` performs - base, the path made
        repo-relative and escaped as `quote(path, safe="/")`, `#` and the row's line
        - written out as the page format specifies it. The other side is the real
        `source-urls` stage over the same graph.
        """
        page_config, rows, _ = self._build()
        bases = page_config[explorer.SOURCE_BASES_KEY]
        single = set(page_config.get(explorer.SOURCE_SINGLE_ROOT_KEY, []))
        graph = self._graph_urls()

        joined = {}
        for row in rows:
            repo, path, line = row[1], row[2], row[10]
            inside = source_urls.repo_relative(repo, path, repo in single)
            link = None
            if repo in bases and inside is not None:
                link = bases[repo] + quote(inside, safe="/") + (f"#{line}" if line else "")
            joined[(repo, path)] = link

        self.assertEqual(joined, graph)
        # Not vacuous: the four files inside a linked repository link, each at its
        # line, and the other three do not - counted by hand from NODES.
        self.assertEqual(sum(1 for link in joined.values() if link), 4)
        self.assertEqual(
            joined[("example-repo", "src/odd name #2 (draft).ts")],
            f"https://github.com/example-org/example-repo/blob/{SHA}/"
            "src/odd%20name%20%232%20%28draft%29.ts#L3-L7",
        )
        self.assertEqual(
            joined[("single-repo", "repositories/single-repo/lib/café.ts")],
            f"https://github.com/example-org/single-repo/blob/{OTHER_SHA}/lib/caf%C3%A9.ts#L1",
        )

    def test_a_row_that_gets_no_link_carries_no_line(self):
        """Breaks if every row is given its line whether or not it can be linked.

        The anchor is the one per-row cost of linking, and a row the page will never
        link - another host, no recorded commit, a path outside its repository -
        would pay it for nothing. Every node in this graph names a line, so an empty
        anchor is the page's decision, not the graph's.
        """
        _, rows, _ = self._build()
        lines = {row[2]: row[10] for row in rows}

        self.assertEqual(
            lines,
            {
                "src/a.ts": "L10",
                "src/odd name #2 (draft).ts": "L3-L7",
                "../escape.ts": "",
                "repositories/single-repo/src/x.ts": "L4",
                "repositories/single-repo/lib/café.ts": "L1",
                "src/f.ts": "",
                "src/g.ts": "",
            },
        )

    def test_an_unreadable_repository_list_builds_the_page_without_links(self):
        """Breaks if a malformed `config/repositories.txt` either stops the page being
        built, or is read as an estate that links nothing without saying so.

        No link is the safe outcome; a silent one is not, because the operator then
        has a page that quietly stopped linking and nothing naming the cause.
        """
        config.REPOSITORIES_CONFIG.write_text("example-repo|missing-fields\n", encoding="utf-8")

        page_config, rows, stderr = self._build()

        self.assertNotIn(explorer.SOURCE_BASES_KEY, page_config)
        self.assertEqual({row[10] for row in rows}, {""})
        self.assertIn("repositories.txt", stderr)
        self.assertNotIn("provenance.json", stderr)
        # The summary line must not blame the hosts or the shas for it.
        self.assertIn("Source links: none, because the store's link inputs could not", self.stdout)
        self.assertNotIn("github.com clone URL", self.stdout)

    def test_unreadable_provenance_is_named_as_the_file_that_failed(self):
        """Breaks if a malformed `knowledge/provenance.json` is reported as a problem
        with `config/repositories.txt`, sending the operator to the wrong file.

        Asked of the link builder rather than the whole page: the page also reads
        provenance for its sync date, and fails there on a malformed file, which is
        older behaviour this does not change.
        """
        config.PROVENANCE_PATH.write_text("{not json", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(explorer.store_source_urls())

        self.assertIn("provenance.json", err.getvalue())
        self.assertNotIn("repositories.txt", err.getvalue())

    def test_a_file_name_that_cannot_be_encoded_gets_no_line_and_stops_nothing(self):
        """Breaks if a lone surrogate in a `source_file` - what a non-UTF-8 file name
        decodes to under surrogateescape - raises out of the index instead of leaving
        that row unlinked. The other rows of its repository keep their lines."""
        graph = {
            "nodes": [*NODES, _node("h", "example-repo", "src/caf\udcff.ts", "L8")],
            "links": [*LINKS, {"source": "h", "target": "a"}],
        }
        with contextlib.redirect_stderr(io.StringIO()):
            entries, _ = explorer.build_index(
                graph, {}, {}, source_urls.SourceUrls.from_store(), set()
            )
        lines = {entry[2]: entry[10] for entry in entries}

        self.assertEqual(lines["src/caf\udcff.ts"], "")
        self.assertEqual(lines["src/a.ts"], "L10")

    def test_a_repository_name_cannot_close_the_config_block(self):
        """Breaks if #config is written without the `</` escape the other blocks use.

        Repository names are keys of `sourceBases`, and nothing validates a name in
        `config/repositories.txt`; one holding `</script>` would end the block there
        and run what follows it as page script.
        """
        name = "x</script><script>alert(1)</script>"
        config.REPOSITORIES_CONFIG.write_text(
            REPOSITORIES_TXT + f"{name}|git@github.com:example-org/x.git|main\n",
            encoding="utf-8",
        )
        provenance = json.loads(json.dumps(PROVENANCE))
        provenance["repositories"][name] = {"sha": SHA, "branch": "main", "committed": ""}
        store_io.write_json(config.PROVENANCE_PATH, provenance)
        store_io.write_json(
            config.GRAPH_PATH,
            {
                "nodes": [*NODES, _node("h", name, "src/h.ts", "L1")],
                "links": [*LINKS, {"source": "h", "target": "a"}],
            },
        )

        page_config, _, _ = self._build()
        page = config.EXPLORER_PATH.read_text(encoding="utf-8")

        # The block still parses whole, so it ended where it should.
        self.assertEqual(
            page_config[explorer.SOURCE_BASES_KEY][name],
            f"https://github.com/example-org/x/blob/{SHA}/",
        )
        self.assertNotIn("</script><script>alert(1)", page)

    def test_a_sync_after_source_urls_is_warned_about(self):
        """Breaks if the page links one commit while the graph's `url` cites another
        with nothing said. A `sync` between `source-urls` and this stage does that:
        provenance moves on, the graph keeps the links written from the old sha.

        The graph is linked by the real stage function, then example-repo's recorded
        commit moves: its two linked nodes now differ, of the four nodes linked."""
        graph = {"nodes": [dict(node) for node in NODES], "links": LINKS}
        build_source_urls.annotate(graph, source_urls.SourceUrls.from_store())
        store_io.write_json(config.GRAPH_PATH, graph)

        _, _, settled = self._build()
        self.assertNotIn("WARNING", settled)

        moved = json.loads(json.dumps(PROVENANCE))
        moved["repositories"]["example-repo"]["sha"] = "9" * 40
        store_io.write_json(config.PROVENANCE_PATH, moved)
        _, _, drifted = self._build()

        self.assertIn("WARNING: 2 of 4 sampled nodes carry a graph `url` that differs", drifted)
        self.assertIn("knowledgestore source-urls", drifted)
