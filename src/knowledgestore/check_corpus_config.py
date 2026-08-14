"""Report harness configuration carried by the cloned corpus.

The documented workflow clones the estate into `repositories/` inside the store's
own working directory, then runs extraction agents with that directory as their
project root. The corpus is therefore not only data the agents read - it sits
inside the tree the harness itself inspects for its own configuration.

That distinction is the whole issue. The extraction spec already treats file
*contents* as untrusted, and that guidance works: agents that meet
instruction-shaped text report it rather than acting on it. The undefended path is
that some corpus files are configuration the **harness** consumes, not content an
agent chooses to open - an agent never decides to read `.claude/settings.json`,
and hooks declared in one are executed rather than read.

This stage does not remove anything. An `AGENTS.md` in a repository is legitimate
corpus that a question might reasonably be about, and deleting it would make the
store quietly unfaithful to the estate. It reports what is there, so the exposure
is a number somebody has seen rather than a thing nobody looked for.

The real fix is to clone outside the project root, which removes the class rather
than measuring it. Until a store does that, this is the tripwire.

Run:

    knowledgestore check-corpus
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import config

# Files a coding harness may read as its own instructions, rather than as content.
# Kept deliberately broad: an operator enumerating these by hand missed 8 of 18 on
# a real estate, which is the argument for a list that errs wide.
INSTRUCTION_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    "SKILL.md",
    ".cursorrules",
    "copilot-instructions.md",
)

# Directories whose contents a harness may load wholesale.
CONFIG_DIRS = (".claude", ".cursor", ".github/agents")

# The subset that can cause execution rather than instruction.
EXECUTABLE_KEYS = ("hooks", "command")


def _is_executable_config(path: Path) -> bool:
    """True when a settings file declares something the harness would run."""
    if path.name != "settings.json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and any(key in data for key in EXECUTABLE_KEYS)


def _is_config_dir(path: Path) -> bool:
    """A directory a harness loads wholesale, rather than one an agent opens."""
    return any(str(path).endswith(name) for name in CONFIG_DIRS)


def _classify(path: Path, rel: str, found: dict) -> None:
    """Record one corpus path under whichever category it belongs to, if any."""
    if path.is_dir():
        if _is_config_dir(path):
            found["config_dirs"].append(rel)
    elif path.name in INSTRUCTION_FILES:
        found["instructions"].setdefault(path.name, []).append(rel)
    elif _is_executable_config(path):
        found["executable"].append(rel)


def scan(roots: list[Path]) -> dict:
    """Instruction files, config directories and executable settings under `roots`."""
    found: dict = {"instructions": {}, "config_dirs": [], "executable": []}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if ".git" in path.parts:
                continue
            _classify(path, str(path.relative_to(root.parent)), found)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore check-corpus",
        description="Report harness configuration carried by the cloned corpus.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any instruction file or config directory is found, "
        "not only when one declares something executable",
    )
    arguments = parser.parse_args(argv)

    found = scan([config.REPOSITORIES_DIR, config.EXTERNAL_DIR])
    total = sum(len(paths) for paths in found["instructions"].values())

    if not total and not found["config_dirs"]:
        print("No harness configuration found in the corpus.")
        return 0

    print("Harness configuration in the corpus (content, not the store's own settings):")
    for name, paths in sorted(found["instructions"].items()):
        print(f"  {len(paths):>3}  {name}")
        for path in paths[:3]:
            print(f"       {path}")
        if len(paths) > 3:
            print(f"       … and {len(paths) - 3} more")
    for path in found["config_dirs"]:
        print(f"       {path}/")

    if found["executable"]:
        print(
            f"\n{len(found['executable'])} of these declare hooks or commands, which a harness "
            "executes rather than reads:"
        )
        for path in found["executable"]:
            print(f"  - {path}")
        print(
            "\nThese are corpus, not defects - leave them in place. The exposure is that they sit "
            "inside the tree a harness inspects for its own configuration. Clone the corpus "
            "outside the store's working directory to remove it."
        )
        return 1

    print(
        f"\n{total} instruction file(s) present and none declares anything executable. "
        "They are still read as instructions by a harness whose project root contains them."
    )
    return 1 if arguments.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
