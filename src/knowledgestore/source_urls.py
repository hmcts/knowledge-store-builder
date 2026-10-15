"""Links a reader can open, for the files a store cites.

An answer citing `repositories/<repo>/<path>` names a file in the build corpus,
and no store commits its corpus, so nobody reading the answer can follow the
citation. The store already holds everything a followable link needs: each
repository's clone URL in `config/repositories.txt`, and the commit it was built
from in `knowledge/provenance.json`. This module assembles the link once, so an
agent copies it rather than building one and getting the sha or the path wrong:

    https://github.com/<org>/<repo>/blob/<sha>/<path>#L<line>

**Pinned to the provenance sha, never to a branch.** The repositories move on
after a build, and a link to the default branch drifts off the line it cites.

**The form is a contract with stores that wrote it first.** A consuming store
published this field, in this form, from its own enrich step before the library
had one, and answers already cite it. So the line-anchor reading and the path
escaping below are that store's, and a node gets the same bytes from either. The
path is percent-encoded with only `/` kept raw: a `#` in a file name written raw
turns the rest of the path into a fragment and links a different file.

**No link is better than a wrong one.** A repository on another host, a missing
or malformed sha, a repository the store never configured, or a path that is not
inside its own repository gets no URL, and `resolve` says which of those it was.
The reasons are reported, never guessed around.

Both build routes are accepted, and which one a repository came through is
decided per repository, never per path. The per-repository route writes
repo-relative `source_file`s; the single-root route writes
`repositories/<repo>/<path>`, which `io.report_join_cardinality` describes. A
repository is read as single-root only when every one of its nodes with a
`source_file` carries its own prefix (`single_root_repositories`). Deciding per
path misread a repository that really holds a top-level `repositories/`
directory: its own files were stripped to a different path, and its files under
another name were refused.

**The sha is the synced head.** `sync` records it, so a refresh that syncs a
repository without re-extracting it pins the old graph's line numbers to the new
commit. Re-extract after a sync before trusting a line anchor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from . import config, provenance
from .export_git_history import read_repository_config

CORPUS_PREFIX = "repositories/"

# Why a node or file got no URL. Reported by name, so they are stable strings.
NO_SOURCE_FILE = "no source_file"
NO_REPO = "no repo"
NOT_CONFIGURED = "repository not in config/repositories.txt"
NOT_GITHUB = "clone URL is not on github.com"
NO_SHA = "no provenance sha"
MALFORMED_SHA = "malformed provenance sha"
OUTSIDE_REPOSITORY = "source_file is not a path inside its repository"

# The host is anchored on both sides: `notgithub.com` and `github.com.example.com`
# are other hosts, and a link to github.com for them names a repository that is not
# there. Every form a clone URL takes in a repository list: scp-style, HTTPS, SSH.
_GITHUB = re.compile(
    r"""^(?:git@|ssh://git@|https://|http://|git://)github\.com[:/]
        (?P<org>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)
        (?:\.git)?/?$""",
    re.VERBOSE,
)
# A full commit id, SHA-1 or SHA-256. An abbreviated sha can become ambiguous as a
# repository grows, and `head_info` records the full one, so anything shorter is
# not a value this store wrote.
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# The first line a `source_location` names, and a range end directly after it.
# graphify writes `L26`, `L5-L9`, `values.yaml:149`, `a.sql:1-19`, `lines 75, 109`
# and `1-9`; a location naming no line (`variables.tf`, `apps[3]`) gets no anchor.
# This is the consuming store's regex verbatim, quirks included, and they are kept
# on purpose for byte parity with the links it has published: a reversed range
# `L20-L12` stays reversed, `host:8080` reads as line 8080 and `1.2.3` as line 1.
# Do not tidy it without changing that store's links in the same release.
# Sonar S5850: the `^` anchors only the first alternative on purpose - a line is
# either at the start (optionally after "line"/"lines") or straight after a colon.
_LINE = re.compile(r"(?:^(?:lines?\s+)?|:)L?(\d+)(?:\s*-\s*L?(\d+))?")  # NOSONAR(S5850)
# Only the separator is kept raw. Narrower than RFC 3986 needs - `(` would be safe -
# and deliberately so: it is the escaping the consuming store's links already use,
# and two spellings of one link would make the same citation compare unequal.
_PATH_SAFE = "/"


def github_origin(clone_url: str) -> tuple[str, str] | None:
    """(org, repository) of a github.com clone URL, or None for any other host."""
    match = _GITHUB.match(clone_url.strip())
    return (match.group("org"), match.group("repo")) if match else None


def line_anchor(source_location: object) -> str:
    """`#L<n>`, `#L<n>-L<m>`, or "" when the location names no line."""
    if not isinstance(source_location, str):
        return ""
    match = _LINE.search(source_location.strip())
    if not match:
        return ""
    start, end = match.group(1), match.group(2)
    return f"#L{start}-L{end}" if end and end != start else f"#L{start}"


def single_root_repositories(nodes) -> set[str]:
    """Repositories every one of whose file-bearing nodes carries its own corpus prefix.

    That is the single-root route's signature. A repository with even one node whose
    `source_file` lacks `repositories/<repo>/` came through the per-repository route,
    and a path in it that begins `repositories/` is a real directory in the repository.
    """
    prefixed: dict[str, bool] = {}
    for node in nodes:
        repo, path = node.get("repo"), node.get("source_file")
        if not (isinstance(repo, str) and repo and isinstance(path, str) and path):
            continue
        own = path.startswith(f"{CORPUS_PREFIX}{repo}/")
        prefixed[repo] = prefixed.get(repo, True) and own
    return {repo for repo, every in prefixed.items() if every}


def repo_relative(repo: str, path: str, single_root: bool = False) -> str | None:
    """The path inside `repo`, or None when it names no file inside it.

    `single_root` says which route the repository came through, from
    `single_root_repositories`. Only then is `repositories/<repo>/` stripped; on the
    per-repository route the path is already repo-relative, whatever it begins with.
    None for an absolute (in-flight) path or one climbing out with `..`.
    """
    if not path or path.startswith("/"):
        return None
    if single_root:
        own = f"{CORPUS_PREFIX}{repo}/"
        if not path.startswith(own):
            return None
        path = path[len(own) :]
    segments = path.split("/")
    if not path or any(segment in ("", ".", "..") for segment in segments):
        return None
    return path


@dataclass(frozen=True)
class Resolution:
    """A URL, or the reason there is none. Exactly one of the two is set."""

    url: str | None
    reason: str


class SourceUrls:
    """Builds links for one store's repositories.

    Construct from the store with `from_store()`, or directly from the two maps -
    clone URL and provenance sha by repository name - which is the seam tests use.
    """

    def __init__(self, clone_urls: dict[str, str], shas: dict[str, str]):
        self._bases: dict[str, str] = {}
        self._missing: dict[str, str] = {}
        for repo, clone_url in clone_urls.items():
            origin = github_origin(clone_url)
            sha = shas.get(repo)
            if origin is None:
                self._missing[repo] = NOT_GITHUB
            elif not isinstance(sha, str) or not sha:
                self._missing[repo] = NO_SHA
            elif not _SHA.match(sha):
                self._missing[repo] = MALFORMED_SHA
            else:
                org, name = origin
                self._bases[repo] = f"https://github.com/{org}/{name}/blob/{sha}/"

    @classmethod
    def from_store(cls) -> SourceUrls:
        """Read `config/repositories.txt` and `knowledge/provenance.json`.

        Either may be absent, which yields a builder that links nothing and says why.
        Fetch-only repositories are not read: provenance keeps them apart from the
        estate, and no graph node is one of their files. A malformed repository list
        raises `ValueError` from its own parser, naming the line.
        """
        clone_urls: dict[str, str] = {}
        if config.REPOSITORIES_CONFIG.is_file():
            clone_urls = {
                entry.name: entry.clone_url
                for entry in read_repository_config(config.REPOSITORIES_CONFIG)
            }
        shas = {
            name: entry.get("sha", "")
            for name, entry in provenance.read().items()
            if isinstance(entry, dict)
        }
        return cls(clone_urls, shas)

    def base(self, repo: object) -> str | None:
        """`https://github.com/<org>/<repo>/blob/<sha>/`, or None when `repo` gets no link.

        The prefix every link `resolve` writes for this repository, so a reader that
        joins it to an escaped repo-relative path and a line anchor holds the same
        bytes as the node's `url`. The explorer page carries this, one per
        repository, rather than a URL per row: every URL is unique, so a per-row
        column could not be interned and would grow the page by about a third.
        """
        return self._bases.get(repo) if isinstance(repo, str) else None

    def resolve(
        self,
        repo: object,
        path: object,
        source_location: object = None,
        single_root: bool = False,
    ) -> Resolution:
        """The URL for one file, anchored at `source_location`'s line where it names one.

        `single_root` is the repository's route, from `single_root_repositories`.
        """
        if not isinstance(path, str) or not path:
            return Resolution(None, NO_SOURCE_FILE)
        if not isinstance(repo, str) or not repo:
            return Resolution(None, NO_REPO)
        base = self.base(repo)
        if base is None:
            return Resolution(None, self._missing.get(repo, NOT_CONFIGURED))
        inside = repo_relative(repo, path, single_root)
        if inside is None:
            return Resolution(None, OUTSIDE_REPOSITORY)
        return Resolution(base + quote(inside, safe=_PATH_SAFE) + line_anchor(source_location), "")

    def file_url(self, repo: object, path: object) -> str | None:
        """The file-level URL, with no line anchor, or None."""
        return self.resolve(repo, path).url
