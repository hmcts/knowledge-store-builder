"""Build an answers layer from a directory of Markdown.

A store's written answers - journeys, findings, design notes - are usually a
directory of Markdown files, and the MCP server's `answers` tool reads an extra
layer only as one JSON object of entries, `{id: {title, html, source}}`. A store
that declared the directory itself would see the layer reported `not_read`. This
stage writes the file the server reads, and the store declares that file in
`config/store-extra-layers.json` with `"kind": "answers"`, never the directory.

One entry per `.md` file under `--markdown`, keyed by its path relative to it,
without the suffix:

- `title`: the first heading, else the file's stem;
- `html`: the body, front matter removed, rendered by the same Markdown subset as
  topic briefs and deep dives;
- `source`: the file's path relative to the store root;
- `as_of`: the front matter's `date:` when it has one, else the date of the last
  commit to the file;
- `commit`: that commit's SHA, or null for a dated file that is not committed.

**Written answers are point-in-time.** A journey or a finding describes the estate
as it was when it was written, and a served answer must never read as today's
knowledge. So every entry carries `as_of`, and a file with neither a date nor a
commit is refused rather than written undated.

**It refuses, writing nothing**, when `--markdown` holds no `.md` file, when a file
lies outside the store root, when a file has no date, and when an id is not one
plain path segment. The server skips an entry whose id it cannot return as
structure - a file in a subdirectory, or a name with a space - and does not tell
the store, so writing one would publish an answer that is never served.

Two runs at one commit write identical bytes: files are read in sorted order and
the dates come from git, never from the clock or a modification time.

Run: knowledgestore answers-layer --markdown docs/answers --out knowledge/answers.json
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import config, io
from .build_topic_briefs import markdown_to_html
from .git_env import git_env

# The server's rule for an id it returns as structure (`_plain_segment` in
# knowledge-store-mcp `tools_tickets`): letters and digits in any script, `.`,
# `_` and `-`, and not dots alone.
_PUNCTUATION = "._-"


def run_git(arguments: list[str]) -> str:
    """Run git and return stdout. The IO boundary; tests may replace it."""
    return os.fsdecode(
        subprocess.run(
            ["git", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env(),
        ).stdout
    )


def plain_segment(segment: str) -> bool:
    """Whether the server returns `segment` as an id rather than skipping it."""
    return bool(segment.strip(".")) and all(c.isalnum() or c in _PUNCTUATION for c in segment)


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """The front matter's `key: value` lines, and the body after it.

    Front matter opens on the first line with `---` and closes on the next line
    that is `---` or `...`. Without a closing line there is none, and the whole
    text is the body.
    """
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return {}, text
    for end in range(1, len(lines)):
        if lines[end].rstrip() in ("---", "..."):
            fields = {}
            for line in lines[1:end]:
                key, colon, value = line.partition(":")
                if colon:
                    fields[key.strip()] = value.strip().strip("'\"")
            return fields, "\n".join(lines[end + 1 :])
    return {}, text


def first_heading(body: str) -> str | None:
    """The text of the first heading, read the way the renderer reads one."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    return None


def last_commit(path: Path, run=run_git) -> tuple[str, str] | None:
    """(commit date, SHA) of the last commit to `path`, or None if it has none."""
    try:
        output = run(["-C", str(path.parent), "log", "-1", "--format=%H%x1f%cI", "--", path.name])
    except subprocess.CalledProcessError:
        # Not inside a repository: no commit holds the file.
        return None
    # Only the newline: `str.strip()` would also eat the separator (CLAUDE.md 9).
    sha, _, date = output.strip("\n").partition("\x1f")
    if date.endswith("Z"):
        # Newer git writes UTC as `Z` and older git as `+00:00`; one spelling keeps
        # the bytes the same whichever git built the layer.
        date = date[:-1] + "+00:00"
    return (date, sha) if sha else None


def entry(path: Path, source: str, run=run_git) -> dict | None:
    """One answer, or None when nothing says when it was written."""
    fields, body = split_front_matter(path.read_text(encoding="utf-8"))
    committed = last_commit(path, run)
    as_of = fields.get("date") or (committed[0] if committed else None)
    if not as_of:
        return None
    # Keys in sorted order, so the file is the same bytes whatever wrote it.
    return {
        "as_of": as_of,
        "commit": committed[1] if committed else None,
        "html": markdown_to_html(body),
        "source": source,
        "title": first_heading(body) or path.stem,
    }


def build(source_dir: Path, run=run_git) -> tuple[dict[str, dict], list[str]]:
    """The layer, and every reason it cannot be written. Any reason refuses it."""
    root = config.ROOT.resolve()
    files = sorted(p for p in source_dir.resolve().rglob("*.md") if p.is_file())
    if not files:
        return {}, [f"{source_dir} holds no .md files"]
    layer: dict[str, dict] = {}
    problems: list[str] = []
    for path in files:
        answer_id = path.relative_to(source_dir.resolve()).with_suffix("").as_posix()
        if not plain_segment(answer_id):
            problems.append(
                f"{answer_id}: the server skips an id that is not one plain name "
                "(letters, digits, `.`, `_`, `-`), so a file in a subdirectory or with "
                "a space in its name would never be served; move or rename it"
            )
            continue
        if root not in path.parents:
            problems.append(f"{path}: outside the store root {root}")
            continue
        written = entry(path, path.relative_to(root).as_posix(), run)
        if written is None:
            problems.append(
                f"{path.relative_to(root).as_posix()}: no date - commit it, or give it "
                "front matter with `date:`, so the answer says when it was true"
            )
            continue
        layer[answer_id] = written
    return layer, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore answers-layer",
        description="Write the answers layer the MCP server reads from a directory of Markdown.",
    )
    # Not `--from`: the documentation names uv's `--from` in the server's launch
    # block, and a stage declaring it would hide a wrong use of either.
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)

    layer, problems = build(arguments.markdown)
    if problems:
        for problem in problems:
            print(f"refused - {problem}", file=sys.stderr)
        print(f"Nothing was written to {arguments.out}.", file=sys.stderr)
        return 1
    # Entries were added in sorted file order, so the ids are already in order.
    io.write_json(arguments.out, layer, indent=1)
    print(f"{len(layer)} answers -> {arguments.out}")
    return 0
