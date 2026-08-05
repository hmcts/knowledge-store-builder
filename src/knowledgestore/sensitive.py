"""Withhold mined commit text that identifies a specific case or person.

Commit messages are written for colleagues, not for publication. Some of them
describe one particular case: its reference, sometimes the names of the people
involved, sometimes what happened at a hearing. A store commits the text it
mines and its browser page embeds it, so whatever is mined is republished - to
everyone who can read the store, which is a wider audience than the repository
the commit sits in.

**The whole value is dropped, never the matched span.** A commit message that
names a case is describing that case rather than the architecture, so the rest
of the sentence is case narrative too. Redacting only the reference leaves the
account of what happened attached to a ticket that still identifies whose case
it was, which is still personal data - and it looks like diligence while leaving
the disclosure in place. Dropping the value costs a little evidence about the
architecture; the ticket, its dates, its repositories and its file links are all
unaffected, because none of those identifies anybody.

**Personal names are deliberately not detected.** Recognising them in commit
prose is unreliable in both directions, and a rule that half-works invites
reliance on it. Where names have been found, they sat beside a case reference -
a shape a matcher can be certain of - and the value-drop rule removes them with
it. So: this reduces exposure. It does not certify a file as free of personal
data, and no reader, report or downstream stage may treat it as though it did.

The rules are settings (`config.SENSITIVE_PATTERNS`), because identifier formats
differ between estates.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from . import config


# The fields of a ticket record that hold mined commit text. Dates, repositories
# and counts are not text and are never withheld.
EVIDENCE_FIELDS = ("d", "s", "b")

# Cached per rule set so a run over 180,000 commits compiles each rule once.
# Keyed on the rules themselves, not stored as a module constant: `configure()`
# runs after import, and a pattern captured at import would ignore it.
_COMPILED: dict[tuple[tuple[str, str], ...], tuple[tuple[str, re.Pattern[str]], ...]] = {}


def rules() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The withholding rules in force right now, in rule-name order.

    Name-ordered so that a value matching two rules is always reported under the
    same one - two runs on the same inputs have to produce the same report.
    Raises `re.error` for a rule that will not compile, which is the intended
    outcome: an unusable rule must stop the run, not quietly withhold nothing.
    """
    key = tuple(sorted(config.SENSITIVE_PATTERNS.items()))
    if key not in _COMPILED:
        _COMPILED[key] = tuple((rule, re.compile(pattern)) for rule, pattern in key)
    return _COMPILED[key]


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
