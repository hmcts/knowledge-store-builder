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

Both build routes are accepted. The per-repository route writes repo-relative
`source_file`s; the single-root route writes `repositories/<repo>/<path>`, which
`io.report_join_cardinality` describes. The prefix is stripped only when it names
the node's own repository.
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
_LINE = re.compile(r"(?:^(?:lines?\s+)?|:)L?(\d+)(?:\s*-\s*L?(\d+))?")
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


def repo_relative(repo: str, path: str) -> str | None:
    """The path inside `repo`, from either build route's form, or None.

    None for anything that does not name a file inside this repository: a corpus
    prefix naming a different repository, an absolute (in-flight) path, or a path
    climbing out with `..`. Linking any of those would name the wrong file.
    """
    if not path or path.startswith("/"):
        return None
    if path.startswith(CORPUS_PREFIX):
        owner, _, path = path[len(CORPUS_PREFIX) :].partition("/")
        if owner != repo:
            return None
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
            elif not isinstance(sha, str) or not _SHA.match(sha):
                self._missing[repo] = NO_SHA
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

    def resolve(self, repo: object, path: object, source_location: object = None) -> Resolution:
        """The URL for one file, anchored at `source_location`'s line where it names one."""
        if not isinstance(path, str) or not path:
            return Resolution(None, NO_SOURCE_FILE)
        if not isinstance(repo, str) or not repo:
            return Resolution(None, NO_REPO)
        base = self._bases.get(repo)
        if base is None:
            return Resolution(None, self._missing.get(repo, NOT_CONFIGURED))
        inside = repo_relative(repo, path)
        if inside is None:
            return Resolution(None, OUTSIDE_REPOSITORY)
        return Resolution(base + quote(inside, safe=_PATH_SAFE) + line_anchor(source_location), "")

    def file_url(self, repo: object, path: object) -> str | None:
        """The file-level URL, with no line anchor, or None."""
        return self.resolve(repo, path).url
