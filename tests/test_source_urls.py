"""Links a reader can open, for the files a store cites.

An answer citing `repositories/<repo>/<path>` names a file in the build corpus,
which no store commits, so nobody reading the answer can follow it. The store
already holds what a followable link needs - the repository's clone URL and the
commit it was built from - and `source_urls` assembles it once, so an agent copies
a link instead of building one and getting the sha or the path wrong.

Expected values are written out by hand. Each test names the defect it catches.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated
from knowledgestore import config, provenance, source_urls

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"

# The header and line format `knowledgestore discover` writes, verbatim.
REPOSITORIES_TXT = """\
# Generated from the example-org organisation repository listing
# Filtered by config/repository-filters.txt (archived always excluded)
# Format: name|clone-url|default-branch
example-repo|git@github.com:example-org/example-repo.git|main
renamed-locally|https://github.com/example-org/upstream-name.git|master
hosted-elsewhere|git@gitlab.example.com:example-org/hosted-elsewhere.git|main
never-synced|git@github.com:example-org/never-synced.git|main
"""


class ParsingTheCloneUrl(unittest.TestCase):
    def test_every_github_clone_url_form_names_its_org_and_repository(self):
        """Breaks if one clone URL form is not recognised. `discover` writes the scp
        form, and an operator's hand-written list may use any of the others."""
        for url in (
            "git@github.com:example-org/example-repo.git",
            "git@github.com:example-org/example-repo",
            "https://github.com/example-org/example-repo.git",
            "https://github.com/example-org/example-repo",
            "https://github.com/example-org/example-repo/",
            "ssh://git@github.com/example-org/example-repo.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(source_urls.github_origin(url), ("example-org", "example-repo"))

    def test_a_clone_url_on_another_host_names_nothing(self):
        """Breaks if the host is not checked: a GitLab or self-hosted repository
        would get a github.com link to a repository that does not exist there."""
        for url in (
            "git@gitlab.example.com:example-org/example-repo.git",
            "https://gitlab.example.com/example-org/example-repo.git",
            "https://notgithub.com/example-org/example-repo.git",
            "https://github.com.example.com/example-org/example-repo.git",
            "https://github.com/example-org",
            "",
        ):
            with self.subTest(url=url):
                self.assertIsNone(source_urls.github_origin(url))


class TheLineAnchor(unittest.TestCase):
    def test_every_location_shape_graphify_writes_reads_as_its_first_line(self):
        """Breaks if a shape that names a line loses its anchor, or one that names
        none gains one. The shapes are those a real merged graph carries, and the
        readings match the consuming store that wrote this field first."""
        cases = [
            ("L26", "#L26"),
            ("L12-L20", "#L12-L20"),
            ("L9-12", "#L9-L12"),
            ("L5-L5", "#L5"),
            ("values.yaml:149", "#L149"),
            ("values.yaml:L1", "#L1"),
            ("payment.sql:1-19", "#L1-L19"),
            ("lines 75, 109, 116", "#L75"),
            ("line 4", "#L4"),
            ("1-9", "#L1-L9"),
            ("variables.tf", ""),
            ("components.schemas.SearchResponse_status", ""),
            ("apps[3]", ""),
            ("", ""),
            (None, ""),
        ]
        for location, anchor in cases:
            with self.subTest(location=location):
                self.assertEqual(source_urls.line_anchor(location), anchor)


class TheRepositoryRelativePath(unittest.TestCase):
    def test_both_build_routes_name_the_same_file(self):
        """Breaks if the single-root route's prefix is kept: the link would point at
        `repositories/<repo>/...` inside the repository, which does not exist."""
        self.assertEqual(source_urls.repo_relative("example-repo", "src/app.py"), "src/app.py")
        self.assertEqual(
            source_urls.repo_relative(
                "example-repo", "repositories/example-repo/src/app.py", single_root=True
            ),
            "src/app.py",
        )

    def test_a_per_repository_path_beginning_repositories_is_a_real_path(self):
        """Breaks if a repository that really holds a `repositories/` directory has
        its paths read as the corpus prefix: its own name stripped to a different
        file, another name refused."""
        for path in ("repositories/example-repo/index.md", "repositories/other-repo/notes.md"):
            with self.subTest(path=path):
                self.assertEqual(source_urls.repo_relative("example-repo", path), path)

    def test_a_path_that_is_not_inside_its_own_repository_names_nothing(self):
        """Breaks if a single-root path naming a different repository is stripped
        anyway, which would link one repository's file under another's name."""
        for path in (
            "repositories/other-repo/src/app.py",
            "repositories/example-repo",
            "repositories/example-repo/",
            "src/app.py",
        ):
            with self.subTest(path=path, route="single-root"):
                self.assertIsNone(source_urls.repo_relative("example-repo", path, single_root=True))
        for path in (
            "/home/build/store/repositories/example-repo/src/app.py",
            "../example-repo/src/app.py",
            "src/../../escape.py",
            "",
        ):
            with self.subTest(path=path, route="per-repository"):
                self.assertIsNone(source_urls.repo_relative("example-repo", path))


class TheBuildRoute(unittest.TestCase):
    def test_a_repository_is_single_root_only_when_every_file_carries_its_prefix(self):
        """Breaks if one prefixed path is enough to call a repository single-root,
        which is the per-path misreading: a real `repositories/` directory in a
        per-repository build would have its paths stripped."""
        nodes = [
            {"repo": "single", "source_file": "repositories/single/a.py"},
            {"repo": "single", "source_file": "repositories/single/b/c.py"},
            {"repo": "single"},  # a package node with no file decides nothing
            {"repo": "mixed", "source_file": "repositories/mixed/a.py"},
            {"repo": "mixed", "source_file": "src/b.py"},
            {"repo": "plain", "source_file": "src/a.py"},
            {"repo": "foreign", "source_file": "repositories/other/a.py"},
            {"repo": "", "source_file": "repositories/x/a.py"},
        ]
        self.assertEqual(source_urls.single_root_repositories(nodes), {"single"})


def _builder() -> source_urls.SourceUrls:
    return source_urls.SourceUrls(
        clone_urls={
            "example-repo": "git@github.com:example-org/example-repo.git",
            "renamed-locally": "https://github.com/example-org/upstream-name.git",
            "hosted-elsewhere": "git@gitlab.example.com:example-org/hosted-elsewhere.git",
            "never-synced": "git@github.com:example-org/never-synced.git",
            "bad-sha": "git@github.com:example-org/bad-sha.git",
            "short-sha": "git@github.com:example-org/short-sha.git",
        },
        shas={
            "example-repo": SHA,
            "renamed-locally": OTHER_SHA,
            "hosted-elsewhere": SHA,
            "bad-sha": "",
            "short-sha": "0123abc",
            "unconfigured": SHA,
        },
    )


class BuildingTheUrl(unittest.TestCase):
    def test_a_node_links_to_its_line_at_the_build_commit(self):
        """Breaks if any part of the link drifts from the form consuming stores
        already write: host, org, repo, `blob`, the provenance sha, path, anchor."""
        got = _builder().resolve("example-repo", "src/app/case.py", "L12-L20")
        self.assertEqual(
            got.url,
            "https://github.com/example-org/example-repo/blob/"
            "0123456789abcdef0123456789abcdef01234567/src/app/case.py#L12-L20",
        )
        self.assertEqual(got.reason, "")

    def test_both_path_forms_give_one_url(self):
        """Breaks if the single-root route gets a different (wrong) link."""
        urls = _builder()
        self.assertEqual(
            urls.resolve(
                "example-repo", "repositories/example-repo/src/a.py", single_root=True
            ).url,
            urls.resolve("example-repo", "src/a.py").url,
        )

    def test_the_org_and_name_come_from_the_clone_url_not_the_local_name(self):
        """Breaks if the local name stands in for the repository's name on the host:
        a repository listed under another name would link to a 404."""
        self.assertEqual(
            _builder().file_url("renamed-locally", "README.md"),
            f"https://github.com/example-org/upstream-name/blob/{OTHER_SHA}/README.md",
        )

    def test_a_file_url_never_carries_a_line_anchor(self):
        """Breaks if a per-file artefact is given a line it never named."""
        self.assertEqual(
            _builder().file_url("example-repo", "src/a.py"),
            f"https://github.com/example-org/example-repo/blob/{SHA}/src/a.py",
        )

    def test_a_character_that_would_change_the_url_is_escaped(self):
        """Breaks if a `#` or a space in a path is written raw: the `#` would cut the
        path into a fragment and link a different file, which is a wrong URL rather
        than a missing one."""
        self.assertEqual(
            _builder().file_url("example-repo", "docs/release notes#2.md"),
            f"https://github.com/example-org/example-repo/blob/{SHA}/docs/release%20notes%232.md",
        )

    def test_the_escaping_matches_the_links_stores_already_publish(self):
        """Breaks if a wider safe set is adopted. `(` is legal raw in a URL path, but
        the consuming store's committed links encode it, and one file spelled two
        ways makes the same citation compare unequal across the two writers."""
        self.assertEqual(
            _builder().file_url("example-repo", "db/Example Table (Draft).sql"),
            f"https://github.com/example-org/example-repo/blob/{SHA}/db/Example%20Table%20%28Draft%29.sql",
        )

    def test_the_base_is_the_prefix_every_link_for_the_repository_starts_with(self):
        """Breaks if `base` is built a second way and drifts from the links `resolve`
        writes, or offers a base where `resolve` would refuse the repository.

        The explorer page carries the base once per repository and joins each row to
        it (#423), so a base that is not the prefix of the node's `url` links every
        row of that repository somewhere the graph does not.
        """
        urls = _builder()
        self.assertEqual(
            urls.base("example-repo"),
            f"https://github.com/example-org/example-repo/blob/{SHA}/",
        )
        self.assertEqual(
            urls.base("renamed-locally"),
            f"https://github.com/example-org/upstream-name/blob/{OTHER_SHA}/",
        )
        for repo in ("hosted-elsewhere", "never-synced", "short-sha", "unconfigured", None, 3):
            with self.subTest(repo=repo):
                self.assertIsNone(urls.base(repo))

    def test_no_url_is_written_where_one_cannot_be_right(self):
        """Breaks if any missing input is papered over - a guessed host, an empty sha,
        or a repository the store never configured. Each says why."""
        urls = _builder()
        cases = {
            ("hosted-elsewhere", "src/a.py"): source_urls.NOT_GITHUB,
            ("never-synced", "src/a.py"): source_urls.NO_SHA,
            ("bad-sha", "src/a.py"): source_urls.NO_SHA,
            # Its own reason: "no sha" beside a provenance file that has one sends an
            # operator to look in the wrong place.
            ("short-sha", "src/a.py"): source_urls.MALFORMED_SHA,
            ("unconfigured", "src/a.py"): source_urls.NOT_CONFIGURED,
            ("example-repo", "../a.py"): source_urls.OUTSIDE_REPOSITORY,
            # A lone surrogate, as a non-UTF-8 file name decodes under
            # surrogateescape: `quote` cannot encode it, and must not raise.
            ("example-repo", "src/caf\udcff.py"): source_urls.NOT_ENCODABLE,
        }
        for (repo, path), reason in cases.items():
            with self.subTest(repo=repo, path=path):
                got = urls.resolve(repo, path, "L3")
                self.assertIsNone(got.url)
                self.assertEqual(got.reason, reason)


class ParsingALink(unittest.TestCase):
    """`parse_blob_url` is the inverse of `resolve`, so the citation gate reads a link
    with the same rules that wrote it rather than a second parser of its own."""

    def test_a_link_the_builder_writes_parses_back_to_its_parts(self):
        """Breaks if the parser stops inverting the builder: a path left
        percent-encoded, an anchor read as part of the path, or the local name taken
        for the name on the host. Each makes a real citation look made up."""
        urls = _builder()
        cases = {
            ("example-repo", "src/app/case.py", "L12-L20"): (
                "example-org",
                "example-repo",
                SHA,
                "src/app/case.py",
                "L12-L20",
            ),
            ("example-repo", "docs/release notes#2.md", None): (
                "example-org",
                "example-repo",
                SHA,
                "docs/release notes#2.md",
                "",
            ),
            ("renamed-locally", "db/Example Table (Draft).sql", "3"): (
                "example-org",
                "upstream-name",
                OTHER_SHA,
                "db/Example Table (Draft).sql",
                "L3",
            ),
        }
        for (repo, path, location), expected in cases.items():
            with self.subTest(path=path):
                url = urls.resolve(repo, path, location).url
                assert url is not None
                got = source_urls.parse_blob_url(url)
                assert got is not None
                self.assertEqual(got, source_urls.BlobUrl(*expected))

    def test_only_a_github_blob_link_is_parsed(self):
        """Breaks if the parser accepts a link the gate cannot check against the
        graph - another host, a directory, a raw file - and so reports a link to
        something real as a file the store does not hold."""
        for url in (
            "https://gitlab.example.com/example-org/example-repo/blob/main/a.py",
            "https://github.com.example.com/example-org/example-repo/blob/main/a.py",
            "https://github.com/example-org/example-repo/tree/main/src",
            "https://github.com/example-org/example-repo/blob/main/",
            "https://raw.githubusercontent.com/example-org/example-repo/main/a.py",
            "https://github.com/example-org/example-repo",
            "http://github.com/example-org/example-repo/blob/main/a.py",
            # A control character decoded into the path: no file is named so, and a
            # NUL cannot be handed to the git subprocess the gate asks.
            f"https://github.com/example-org/example-repo/blob/{SHA}/a%00b.py",
            f"https://github.com/example-org/example-repo/blob/{SHA}/a%0Ab.py",
            f"https://github.com/example-org/example-repo/blob/{SHA}/a%7Fb.py",
            "src/app/case.py",
            "",
        ):
            with self.subTest(url=url):
                self.assertIsNone(source_urls.parse_blob_url(url))

    def test_a_link_pinned_to_a_branch_still_parses(self):
        """Breaks if only a full sha is read as a blob link. A made-up or unpinned
        link (`blob/main/...`, an abbreviated sha) would then fall back to being
        skipped, which is the hole the gate exists to close."""
        for ref in ("main", "0123abc"):
            with self.subTest(ref=ref):
                got = source_urls.parse_blob_url(
                    f"https://github.com/example-org/example-repo/blob/{ref}/src/a.py#L4"
                )
                assert got is not None
                self.assertEqual(
                    got, source_urls.BlobUrl("example-org", "example-repo", ref, "src/a.py", "L4")
                )

    def test_a_link_names_the_configured_repository_whatever_its_local_name(self):
        """Breaks if the host's name is looked up as the local name, or case
        matters: a renamed repository's real links would read as foreign, and GitHub
        treats `Example-Org` and `example-org` as one organisation."""
        urls = _builder()
        self.assertEqual(urls.repositories_at("example-org", "upstream-name"), ("renamed-locally",))
        self.assertEqual(urls.repositories_at("Example-Org", "Example-Repo"), ("example-repo",))
        self.assertEqual(urls.repositories_at("example-org", "renamed-locally"), ())
        self.assertEqual(urls.repositories_at("example-org", "hosted-elsewhere"), ())
        self.assertEqual(urls.build_sha("renamed-locally"), OTHER_SHA)
        self.assertEqual(urls.build_sha("never-synced"), "")


class ReadingTheStore(SettingsIsolated):
    def test_the_store_files_are_read_in_the_formats_their_stages_write(self):
        """Breaks if either input is read from the wrong place or in the wrong shape:
        `repositories.txt` as `discover` writes it and provenance as `sync` does."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.configure(root=str(root))
            config.REPOSITORIES_CONFIG.parent.mkdir(parents=True)
            config.REPOSITORIES_CONFIG.write_text(REPOSITORIES_TXT, encoding="utf-8")
            provenance.write(
                {
                    "example-repo": {"sha": SHA, "branch": "main", "committed": "2026-01-02"},
                    "renamed-locally": {"sha": OTHER_SHA, "branch": "master", "committed": ""},
                },
                external={"never-synced": {"sha": SHA, "branch": "main", "committed": ""}},
            )
            urls = source_urls.SourceUrls.from_store()
        self.assertEqual(
            urls.file_url("example-repo", "a.py"),
            f"https://github.com/example-org/example-repo/blob/{SHA}/a.py",
        )
        self.assertEqual(
            urls.file_url("renamed-locally", "a.py"),
            f"https://github.com/example-org/upstream-name/blob/{OTHER_SHA}/a.py",
        )
        # A fetch-only repository's sha is not the estate's: it is recorded apart
        # from `repositories` for that reason, and a node is never one of its files.
        self.assertEqual(urls.resolve("never-synced", "a.py").reason, source_urls.NO_SHA)

    def test_a_store_with_neither_file_links_nothing_and_says_why(self):
        """Breaks if a store without its config or provenance raises instead of
        reporting, or invents a link from nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=str(tmp))
            urls = source_urls.SourceUrls.from_store()
        self.assertEqual(urls.resolve("example-repo", "a.py").reason, source_urls.NOT_CONFIGURED)


class TheSkillCarriesTheCitationRule(unittest.TestCase):
    """CLAUDE.md 3: an agent reads the skill it was invoked with, so a rule that
    lives only in the context file or the docs binds nobody answering a question."""

    SKILL = Path(__file__).resolve().parent.parent / "skills/knowledge-store/SKILL.md"

    def test_the_question_skill_says_cite_the_url_and_never_the_corpus(self):
        """Breaks if the skill goes back to telling agents to cite `source_file`
        alone, which in a single-root store is a `repositories/...` corpus path."""
        text = " ".join(self.SKILL.read_text(encoding="utf-8").split())
        self.assertIn("In an answer or an exported finding, cite a node's `url`", text)
        self.assertIn("never build one yourself", text)
        self.assertIn("https://github.com/<org>/<repo>/blob/<provenance sha>/<path>#L<line>", text)
        self.assertIn("**Never cite `repositories/<repo>/...`**", text)
        self.assertNotIn("Cite `source_file` / `source_location` for specific claims", text)


if __name__ == "__main__":
    unittest.main()
