"""Read one named section out of a document, for the gates that pin its content.

Several documents here state a rule an operator acts on, and prose has no gate
unless one is written. Each of those gates works the same way: find the heading,
take its body up to the next heading, and assert the elements the rule cannot
survive losing are still in it. Each was modelled on the one before it, so by
the third the machinery existed three times -- and three copies of a checker is
three places a fix has to land, which is how one of them ends up subtly
different from the two it was copied from.

What lives here is the machinery only. **The headings, the pinned fragments and
the assertions stay in the module that guards each document**: those are the
substance of each gate, and centralising them would put one document's rules
where another document's test can edit them.

Extraction rather than a search across the whole document is the point of
`section`. Every one of these gates asserts that a fragment is present, and a
document long enough to state a rule states most short fragments somewhere -- so
a gate that searched the file would pass over a section that had been gutted.
The corollary is that `""` has to be treated as a failure by every caller: it is
what the extractor returns when the heading has been renamed, and it satisfies
no assertion except the one that looks for it.

`sensitivity` is the other half. A gate over prose cannot be mutation-tested
from `src/`, so it has to demonstrate in its own run that it would notice the
document losing an element: it forges the real section once per element and
reports every forge the checker failed to attribute to exactly that element.

Imported as a sibling module -- `from doc_sections import section` -- the way
`settings_isolation` is, because `tests/` has no `__init__.py` and unittest
discovery puts the directory on `sys.path`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What the heading is replaced with to prove the extractor reads the heading it
# was given. Any string that is not the heading would do; a heading rather than
# prose so the forged document is still a document.
RENAMED = "## A Different Heading"


@dataclass(frozen=True)
class Copy:
    """One document's copy of an instruction, and what it must still say.

    Two documents often state the same rule -- a skill an agent executes and a
    guide a human reads -- and either can lose it independently, so each is
    guarded separately with its own fragments in its own register.
    """

    path: str
    heading: str
    required: tuple[str, ...]


@dataclass(frozen=True)
class Sensitivity:
    """What forging the real section, one required element at a time, revealed.

    Two fields rather than one because the failures are different. Anything in
    `already_missing` means the document has already lost an element, so no
    forge below it proves anything. Anything in `undetected` means removing that
    element does not show up as that element going missing, so the gate is not
    pinning it.
    """

    already_missing: list[str]
    undetected: list[str]


def section(text: str, heading: str) -> str:
    """The heading's own body, up to the next heading, or "" if it is gone.

    Headings inside fenced code blocks are not headings -- a shell comment opens
    with `#` too, and these sections end in command blocks that start with one.
    """
    if heading not in text:
        return ""
    body: list[str] = []
    fenced = False
    for line in text.split(heading, 1)[1].splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def body_of(copy: Copy) -> str:
    """The guarded section of one document, read from disk."""
    return section((ROOT / copy.path).read_text(encoding="utf-8"), copy.heading)


def section_after_rename(copy: Copy) -> str:
    """What the extractor finds when the document no longer has that heading.

    Anything but "" means it is not reading the heading it names, and every
    content assertion resting on it could be reading a different section.
    """
    text = (ROOT / copy.path).read_text(encoding="utf-8")
    return section(text.replace(copy.heading, RENAMED), copy.heading)


def section_lines(body: str) -> int:
    """How many lines of section there are; 0 when the heading was gone.

    Length rather than presence because a heading that survives a gutting is the
    likelier failure: a section trimmed to a sentence still satisfies every
    assertion that only asks whether it exists.
    """
    return len(body.strip().splitlines()) if body.strip() else 0


def commands(body: str) -> str:
    """Only what is inside the section's fenced blocks.

    Prose is excluded so a sentence mentioning a command cannot stand in for the
    block that shows it: a block is what an operator copies.
    """
    inside: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif fenced:
            inside.append(line)
    return "\n".join(inside)


def collapsed(text: str) -> str:
    """One line, so a fragment matches across a wrapped line break."""
    return " ".join(text.split())


def missing_elements(text: str, required: tuple[str, ...]) -> list[str]:
    """Which required elements are absent. Separate so sensitivity can call it."""
    flat = collapsed(text)
    return [element for element in required if collapsed(element) not in flat]


def missing_commands(body: str, required: tuple[str, ...]) -> list[str]:
    """Which required command fragments are absent from the section's blocks.

    Raw rather than collapsed, unlike `missing_elements`: whitespace inside a
    command is part of it, and a shell line in a fenced block is not rewrapped.
    """
    block = commands(body)
    return [command for command in required if command not in block]


def sensitivity(body: str, required: tuple[str, ...]) -> Sensitivity:
    """Remove each required element from the real section and see what is noticed.

    Collapsed first: an element that wraps across a line break is not present
    verbatim in the raw section, so a raw replace would match nothing and the
    forge would silently be a no-op that the checker reads as a pass.

    A section that is already missing an element short-circuits, because forging
    from one proves nothing -- removing what is already absent is a no-op, and
    the checker then reports the absence it started with as if the forge had
    caused it.
    """
    flat = collapsed(body)
    already = missing_elements(flat, required)
    if already:
        return Sensitivity(already, [])
    undetected = [
        element
        for element in required
        if missing_elements(flat.replace(collapsed(element), ""), required) != [element]
    ]
    return Sensitivity([], undetected)
