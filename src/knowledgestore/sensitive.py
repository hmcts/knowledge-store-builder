"""Redact identifiers out of mined commit text before it is stored.

Commit messages are written for colleagues, not for publication. Some of them
name one particular matter rather than the software: a case or claim reference,
an email address, a National Insurance number, a postcode. A store commits the
text it mines and its browser page embeds it, so whatever is mined is
republished - to everyone who can read the store, a wider audience than the
repository the commit sits in.

Each match is **replaced in place** with a placeholder naming what was taken -
`[case reference withheld]`, `[email address withheld]` - and the words around it
are kept, so the sentence still says what changed and a reader can see that
something was removed and what kind of thing it was. The placeholder is derived
from the rule name, so an estate that adds a rule gets a placeholder with it.
Every match in a value is replaced, and matches of different rules in the same
value are all replaced.

**What this achieves, and what it does not.** Redaction removes the identifiers
these rules match. It does not remove personal names: name recognition in commit
prose is unreliable in both directions, and names have been found beside
descriptions of proceedings, so a redacted value can still describe an
identifiable person's case with the reference taken out. Read a redacted value,
or a clean `check-evidence` result, as "the identifiers these rules match are
gone" - never as "this holds no personal data".

A value left with nothing but placeholders, punctuation and whitespace is not
stored at all: `[case reference withheld]` alone says nothing about the change
and would sit in the retrieval index as noise.

The rules are settings (`config.SENSITIVE_PATTERNS`), because identifier formats
differ between estates.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator

from . import config


# The fields of a ticket record that hold mined commit text. Dates, repositories
# and counts are not text and are never redacted.
EVIDENCE_FIELDS = ("d", "s", "b")

# Any word character outside a placeholder means the value still says something.
WORD = re.compile(r"\w")

# Cached per rule set so a run over 180,000 commits compiles each rule once.
# Keyed on the rules themselves, not stored as a module constant: `configure()`
# runs after import, and a pattern captured at import would ignore it.
_COMPILED: dict[tuple[tuple[str, str], ...], tuple[tuple[str, re.Pattern[str]], ...]] = {}


def placeholder(rule: str) -> str:
    """What replaces a match, derived from the rule that matched it.

    Derived rather than looked up in a second table: a table would need extending
    for every rule an estate adds, and the entry nobody added would leave a
    reader with an unexplained gap in a sentence. Deliberately words only, so no
    placeholder can match a rule and re-trip the gate on redacted text.
    """
    return f"[{rule.replace('-', ' ')} withheld]"


def rules() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The redaction rules in force right now, in rule-name order.

    Name-ordered so two runs on the same inputs redact in the same sequence and
    produce the same bytes. Raises `re.error` for a rule that will not compile,
    which is the intended outcome: an unusable rule must stop the run, not
    quietly redact nothing.
    """
    key = tuple(sorted(config.SENSITIVE_PATTERNS.items()))
    if key not in _COMPILED:
        _COMPILED[key] = tuple((rule, re.compile(pattern)) for rule, pattern in key)
    return _COMPILED[key]


def redact(text: str, counts: Counter[str] | None = None) -> str:
    """`text` with every match replaced by its placeholder.

    Every rule is applied, and each replaces all of its own matches: one value
    can hold several identifiers of several kinds, and stopping at the first
    would leave the rest in place. `counts` accumulates matches per rule for the
    run report - pass None where text is being examined rather than stored, so
    the same identifier is not counted twice.
    """
    if not text:
        return text
    for rule, pattern in rules():
        text, hits = pattern.subn(placeholder(rule), text)
        if hits and counts is not None:
            counts[rule] += hits
    return text


def is_redaction_only(text: str) -> bool:
    """Whether redaction left no words: only placeholders, punctuation, space.

    Such a value is not evidence about the change, only evidence that something
    was taken - which the run report states once, properly.
    """
    residue = text
    for rule, _ in rules():
        residue = residue.replace(placeholder(rule), " ")
    return not WORD.search(residue)


def matched_rule(text: str) -> str:
    """The rule a value falls foul of, or "" when it falls foul of none."""
    if not text:
        return ""
    for rule, pattern in rules():
        if pattern.search(text):
            return rule
    return ""


def mined_values(records: dict) -> Iterator[tuple[str, str, str]]:
    """Every (ticket, field, value) of mined text in a ticket-descriptions
    artefact, ticket-ordered.

    One reader for the artefact's shape, tolerant of a record or a field that is
    not what this version writes: a store's committed file may predate any given
    release, and a gate that crashes on an older one gates nothing.
    """
    for ticket, record in sorted(records.items()):
        if not isinstance(record, dict):
            continue
        for field in EVIDENCE_FIELDS:
            values = record.get(field)
            if not isinstance(values, list):
                continue
            for value in values:
                yield ticket, field, str(value)


def findings(records: dict) -> list[tuple[str, str, str]]:
    """Every (ticket, field, rule) a stored value falls foul of.

    Never the matched text, and never a fragment of it. This is what a CI gate
    prints, and a gate that puts the value in a build log has published it again
    - to a log with longer retention and a wider audience than the artefact.
    """
    return [
        (ticket, field, rule)
        for ticket, field, value in mined_values(records)
        if (rule := matched_rule(value))
    ]
