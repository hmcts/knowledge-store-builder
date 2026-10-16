"""The git this library needs, and the one check every git-using stage makes.

`sync` converts a clone to the reftable ref format when its remote holds branches
differing only in case (#437). Git older than 2.48 cannot read a converted clone
reliably, and cannot convert one either: `git refs migrate` arrived in 2.46 but
refused any repository holding reflogs until 2.48, and every clone holds them.
So 2.48 is a prerequisite for every machine and CI runner that reads a store's
clones, not only for the one that ran `sync`.

The stages that run git refuse below the floor before doing any git work, from
the CLI dispatch. `status` reports the same finding and never fails (CLAUDE.md
6.7), and `knowledgestore --version` prints it.
"""

from __future__ import annotations

import subprocess

from .git_env import git_env

# The one definition of the floor. `sync` reads it too, for the migration.
FLOOR = (2, 48)

UPGRADE = "Upgrade git (https://git-scm.com/downloads), then check with `git --version`."


def run_git(arguments: list[str]) -> str:
    """Run git and return stdout. The IO boundary; tests replace it."""
    return subprocess.run(
        ["git", *arguments], check=True, text=True, stdout=subprocess.PIPE, env=git_env()
    ).stdout


def parse(output: str) -> tuple[int, int] | None:
    """Major and minor from `git version 2.47.1 (Apple Git-150)`; None if unreadable."""
    words = output.split()
    if words[:2] != ["git", "version"] or len(words) < 3:
        return None
    parts = words[2].split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def _read(run) -> tuple[str, tuple[int, int] | None]:
    """What git said, and the version in it. Read once per call."""
    try:
        output = (run or run_git)(["--version"]).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        return f"(git could not be run: {error})", None
    return output, parse(output)


def _floor() -> str:
    return f"{FLOOR[0]}.{FLOOR[1]}"


def refusal(run=None) -> str | None:
    """Why this git cannot be used, or None when it meets the floor."""
    output, version = _read(run)
    if version is None:
        return (
            f"Cannot tell which git this is: `git --version` said {output!r}. "
            f"This library needs git {_floor()} or newer. {UPGRADE}"
        )
    if version < FLOOR:
        return (
            f"git {version[0]}.{version[1]} is older than {_floor()}, which this library "
            f"needs: a clone `sync` has converted to reftable refs cannot be read by an "
            f"older git, on this machine or on any CI runner. Nothing has run. {UPGRADE}"
        )
    return None


def describe(run=None) -> str:
    """One line saying which git this is and whether it meets the floor."""
    output, version = _read(run)
    if version is None:
        return f"git unknown ({output}) - this library needs {_floor()} or newer"
    found = f"{version[0]}.{version[1]}"
    if version < FLOOR:
        return f"git {found} is below the {_floor()} floor - git-using stages will refuse"
    return f"git {found} meets the {_floor()} floor"
