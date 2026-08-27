#!/usr/bin/env python3
"""Refuse a release whose hand-maintained version numbers disagree with its tag.

    python3 scripts/check_release_versions.py            # tag from GITHUB_REF_NAME
    python3 scripts/check_release_versions.py v0.15.0    # tag given explicitly

Two numbers in this repository are written by hand and derived from nothing:

- `.claude-plugin/plugin.json`'s `version`, which is how a user tells one copy of
  the skills from another - the library reports its own version, the plugin cannot;
- the library floor the build skill declares, which is what an operator installs
  to get the stages that skill documents.

The library version itself is the git tag (hatch-vcs), so no file in the tree
states it and nothing keeps those two in step automatically. The plugin manifest
sat four releases behind exactly this way.

**Why this is a script and not a unit test.** It was a unit test, comparing both
numbers against the newest reachable tag. That made the suite's result depend on
whether a tag existed rather than on the tree it ran against: the same `main`
commit passed before a release and failed after it, with no commit to attribute
the failure to. The tag is a release-time input, so the comparison runs at release
time, from the release job, before it publishes anything. The tree-only half of
the property - that the two numbers agree with *each other* - stays in the suite,
where it needs no tag.

**`ahead` fails here, and passes in the suite.** Not the same question. Before a
tag exists, a number above the newest release is a release being prepared and has
to be allowed, or the bump could never be committed. At release time the tag is
the version being published: a number above it advertises a release that does not
exist, which means either the bump was for a later release or the wrong tag is
being cut. Both are worth stopping. So at release time the rule is equality.

Exit status:

    0  both numbers equal the tag
    1  a number is behind, ahead, absent or unreadable - the tree blocks the release
    2  no usable tag was supplied - the check could not run, which is not a pass
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SUMMARY = "Refuse a release whose hand-maintained version numbers disagree with its tag."

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(".claude-plugin/plugin.json")
BUILD_SKILL = Path("skills/knowledge-store-build/SKILL.md")

# The sentence the build skill states its floor in, and the shape of the release
# tags this project cuts. Anchored: `v1.2` must not be read as 1.2.0, because a
# version silently invented from a partial tag is compared against nothing.
MINIMUM_DECLARATION = re.compile(r"assumes knowledge-store-builder (\d+\.\d+\.\d+) or newer")
VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

STALE = 1
UNUSABLE = 2


def triple(version: str) -> tuple[int, int, int] | None:
    """The comparable part of a version, or None when it is not one.

    None rather than an exception: an unreadable number is one of the states this
    check reports on, and the caller says which file it came from.
    """
    parts = VERSION.match(version.strip())
    if parts is None:
        return None
    return (int(parts[1]), int(parts[2]), int(parts[3]))


def manifest_version(root: Path) -> str | None:
    """The version the plugin manifest declares, if it declares one."""
    body = json.loads(root.joinpath(MANIFEST).read_text(encoding="utf-8"))
    version = body.get("version")
    return version if isinstance(version, str) else None


def declared_minimum(root: Path) -> str | None:
    """The library version the build skill's prose says it needs, if it says one."""
    text = root.joinpath(BUILD_SKILL).read_text(encoding="utf-8")
    found = MINIMUM_DECLARATION.search(text)
    return found.group(1) if found else None


def compared(
    where: str, label: str, number: str | None, tag: str, wanted: tuple[int, int, int]
) -> str | None:
    """The complaint about one number against the tag being released, or None.

    `where` names the file and `label` what that file calls its number, so the
    message says which line to edit and what to put on it without a reader
    opening this script - which is the whole point of failing at release time
    rather than after publishing.

    `wanted` is the tag already parsed, passed in rather than re-derived here so
    that this function has no unparseable-tag case to get wrong: `main` refuses a
    tag it cannot read before any comparison is attempted.
    """
    expected = tag.removeprefix("v")
    if number is None:
        return f"{where} declares no {label} this check can read; it should say {expected}"
    found = triple(number)
    if found is None:
        return (
            f"{where}'s {label} is {number!r}, which is not a version of the form N.N.N; "
            f"it should say {expected}"
        )
    if found < wanted:
        return (
            f"{where}'s {label} is {number}, which is behind the tag {tag} being "
            f"released; it should say {expected}"
        )
    if found > wanted:
        return (
            f"{where}'s {label} is {number}, which is ahead of the tag {tag} being "
            f"released; it should say {expected}"
        )
    return None


def complaints(root: Path, tag: str, wanted: tuple[int, int, int]) -> list[str]:
    """Every number that disagrees with the tag, in file order.

    Both are reported rather than the first, so one omission costs one release
    cycle instead of two.
    """
    found = [
        compared(str(MANIFEST), "version", manifest_version(root), tag, wanted),
        compared(str(BUILD_SKILL), "library minimum", declared_minimum(root), tag, wanted),
    ]
    return [problem for problem in found if problem is not None]


def resolve_tag(given: str | None) -> str | None:
    """The tag being released: the argument, else GITHUB_REF_NAME, else None.

    The argument wins so a local run reports on what it was asked about rather
    than on whatever the shell happens to hold. The release job relies on the
    fallback: GITHUB_REF_NAME is a default Actions variable and on a published
    release it is the tag.
    """
    return given or os.environ.get("GITHUB_REF_NAME") or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument(
        "tag",
        nargs="?",
        help="the release tag to compare against; defaults to $GITHUB_REF_NAME",
    )
    parser.add_argument(
        "--root",
        default=str(ROOT),
        help="the checkout to read; defaults to the repository this script is in",
    )
    arguments = parser.parse_args(argv)

    tag = resolve_tag(arguments.tag)
    if tag is None:
        print(
            "No release tag to compare against: none was given as an argument and "
            "GITHUB_REF_NAME is unset. On a published release the release job supplies "
            "it; run this locally as `check_release_versions.py v0.0.0`.",
            file=sys.stderr,
        )
        return UNUSABLE
    wanted = triple(tag)
    if wanted is None:
        print(
            f"{tag!r} is not a release tag of the form vN.N.N, so there is nothing to "
            "compare the plugin manifest and the build skill against.",
            file=sys.stderr,
        )
        return UNUSABLE

    problems = complaints(Path(arguments.root), tag, wanted)
    if problems:
        print(
            f"Release {tag} is blocked: a hand-maintained version disagrees with it.",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return STALE

    print(f"{MANIFEST} and {BUILD_SKILL} both say {tag.removeprefix('v')}, matching {tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
