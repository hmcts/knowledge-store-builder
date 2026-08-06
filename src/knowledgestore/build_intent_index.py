"""Build the file -> Jira-ticket intent index from the Git-history datasets.

For every source file in the estate, records which Jira tickets the non-merge
commits that touched it name, with first/last touch dates. Ticket ids and
descriptions come from the commit subject, and - where the subject offers
nothing - from the commit body, once that body has been reduced to prose.
Subjects and bodies are also kept as fields of their own, so evidence a curated
description cannot carry is still in the artefact. Identifiers naming a specific
case or person are redacted out of subjects and bodies before anything else reads
them; `sensitive.py` holds that rule, what it achieves and what it does not.
This answers "what business change shaped this file?" as a lookup, e.g.:

    import gzip, json
    index = json.load(gzip.open('knowledge/intent/file-tickets.json.gz', 'rt'))
    index['cpp-ui-defence']['src/app/shared/pipes/address.pipe.ts']

Requires knowledge/git-history/ (regenerate with
`knowledgestore export-history` if absent). The output is small and
committed, so consumers get intent lookups without regenerating history.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


from . import config, io, sensitive


TICKET_PREFIX = re.compile(r"^[\s\[\(]*[A-Z][A-Z0-9]{1,9}-\d{1,6}[\]\)]*[\s:,\-–]*")
JUNK_DESCRIPTION = re.compile(
    r"^(wip|fix(es|ed)?|updated?|changes?|test(s|ing)?|merge.*|minor.*|"
    r"address(ed|ing)? (pr |review )?comments?.*|pr comments?.*|refactor(ing)?)$",
    re.IGNORECASE,
)
MIN_DESCRIPTION_CHARS = 12

# Commit bodies. Most are not prose: trailers, separators, a merge's list of
# commits, a bot's dependency metadata, or the authors of a squashed range. Each
# rule below removes one of those, so what survives is something a person wrote
# about the change.
TRAILER_PREFIXES = (
    "co-authored-by:",
    "signed-off-by:",
    "reviewed-by:",
    "change-id:",
    "cherry-picked:",
    "on-behalf-of:",
)
SEPARATOR_LINE = re.compile(r"^[-=]{3,}$")
# Trailers are recognised by shape as well as by the names above, because that
# list can never hold every trailer a team invents, and the learned-boilerplate
# filter cannot rescue the miss: a trailer whose value is a unique hash never
# repeats, so it never crosses the repetition thresholds. Measured on one estate,
# bodies that were nothing but trailer lines were the majority of the bodies
# surviving cleaning - migration metadata stored as intent evidence. Same move as
# identity-not-phrasing and repetition-not-patterns: match the shape, not a name.
# No `\s*` before the value: it would overlap with `(.*)`, which is ambiguous
# enough to backtrack. The value is stripped in code instead.
TRAILER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]{1,30}):(.*)$")
# `KEY: what changed` is content, not a trailer. A bare uppercase token reads as a
# reference - a ticket, a static-analysis rule id - and those lines are among the
# most valuable a body can carry, so the shape test must never claim them.
REFERENCE_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
BOT_LINE = re.compile(
    r"^(bumps \[|dependency-type:|- \[release notes\]|updated-dependencies:)", re.IGNORECASE
)
BOT_NAME = re.compile(r"dependabot", re.IGNORECASE)
AUTHOR_LINE = "author: "

# A body is evidence only if a person wrote it, and the reliable signal is who
# the commit says wrote it - not what the body says. Text markers catch the
# phrasing of one bot; identity catches automation as a class, including bots
# this estate has not met yet. Renovate writes "Update dependency X to v2", so
# a filter tuned to Dependabot's "Bumps [" would let all of it through.
BOT_ACCOUNT = "[bot]"
# Automation predating or outside the GitHub App [bot] convention. Whole-word
# matched, so `snyk` also catches `snyk-bot`.
# Deliberately not a module-level constant. Stage modules in this library read
# `config` when called, never when imported, because `configure()` runs after
# import and a constant captured at import silently ignores it - a defect this
# codebase has already paid for once. Cached per list so 180,000 commits do not
# recompile it.
_IDENTITY_PATTERNS: dict[tuple[str, ...], re.Pattern[str] | None] = {}


def _identity_pattern() -> re.Pattern[str] | None:
    """The automation-identity matcher for the settings in force right now.

    None when the list is empty, which leaves only the `[bot]` convention.
    """
    names = tuple(config.AUTOMATION_IDENTITIES)
    if names not in _IDENTITY_PATTERNS:
        _IDENTITY_PATTERNS[names] = (
            re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b", re.IGNORECASE)
            if names
            else None
        )
    return _IDENTITY_PATTERNS[names]


MERGE_BULLET = "* "
MIN_MERGE_BULLETS = 2
MIN_BODY_CHARS = 25
# One body must not attribute a file to every ticket it happens to list.
BODY_TICKET_LIMIT = 3
BODY_DESCRIPTION_CHARS = 300

# Per ticket, how much of the commit's own words to keep. `d` is a curated
# description and stays at two; these are the evidence fields, where more is
# better because each entry is a different commit's account of the work.
SUBJECT_LIMIT = 3
BODY_LIMIT = 2
# Deliberately not BODY_DESCRIPTION_CHARS, and the two must not be tidied back
# together: a description field keeps a *label*, which a consumer renders on one
# line, while an evidence field keeps the *rationale* - and the rationale is
# precisely what a label-sized cut removes, because the opening line is the
# summary and the reasoning follows it. Breaking-change notices, field renames and
# decision cross-references all sit past character 300. Measured on one estate, a
# 300-character cut discarded 34.1% of all body prose while 4,000 discards 3.6%,
# and the 99th-percentile body is 1,359 characters - so this bounds a pathological
# body, a pasted stack trace or file, rather than summarising anything.
BODY_EVIDENCE_CHARS = 4000

# Contributor and pull-request templates differ between teams and between
# estates, so no shipped pattern list can recognise them. They are instead
# learned from the repository being indexed: a template line repeats verbatim
# across commits, and genuine prose does not. Both thresholds must hold, so a
# small repository cannot have real sentences mistaken for boilerplate.
BOILERPLATE_MIN_COMMITS = 5
BOILERPLATE_MIN_PERCENT = 2
BOILERPLATE_MIN_LINE_CHARS = 8


@dataclass
class BodyReport:
    """Why commit bodies were discarded, so a run can say which net caught what."""

    boilerplate_lines: int = 0
    boilerplate_emptied: int = 0
    automated: int = 0
    bot_text: int = 0
    # Named, not just counted: the identity list is matched as a whole word and
    # several entries are also surnames, so an operator must be able to see a
    # person in this list rather than discover the loss from a missing answer.
    automated_identities: Counter[str] = field(default_factory=Counter)


@dataclass
class RedactionReport:
    """What redaction took out, so a run can say so rather than filter silently."""

    by_rule: Counter[str] = field(default_factory=Counter)
    # Values whose every word was an identifier, so redaction left nothing to
    # store. Counted separately: text changed and text lost are different facts.
    emptied: int = 0


def clean_description(subject: str) -> str:
    """Strip leading ticket references, leaving the human description."""
    cleaned = subject.strip()
    while True:
        stripped = TICKET_PREFIX.sub("", cleaned, count=1)
        if stripped == cleaned:
            break
        cleaned = stripped.strip()
    return cleaned.strip(" .")


def normalise_line(line: str) -> str:
    """The counting key for a body line: case- and whitespace-insensitive.

    Only ever a key. A line that survives keeps its original text.
    """
    return re.sub(r"\s+", " ", line.strip()).lower()


def count_body_lines(ndjson: Path) -> tuple[dict[str, int], int]:
    """Per normalised body line, how many of a repository's commits carry it,
    and how many of its commits have a body at all.

    Counted on redacted text, because the second pass filters redacted lines
    against these keys: learn `Contact team@example.example for help` and the
    redacted line would no longer match it, so a template would survive as
    prose. Redacting here costs a pass and keeps the two halves in step. No
    counting - the same identifiers are counted once, where they are stored.
    """
    counts: dict[str, int] = defaultdict(int)
    bodies = 0
    with ndjson.open(encoding="utf-8") as source:
        for line in source:
            body = sensitive.redact(str(json.loads(line).get("body") or ""))
            if not body.strip():
                continue
            bodies += 1
            keys = {normalise_line(text) for text in body.splitlines()}
            for key in sorted(keys):
                if len(key) >= BOILERPLATE_MIN_LINE_CHARS:
                    counts[key] += 1
    return dict(counts), bodies


def learn_boilerplate(counts: dict[str, int], bodies: int) -> frozenset[str]:
    """The normalised lines a repository repeats often enough to be a template.

    A line must appear in at least BOILERPLATE_MIN_COMMITS commits *and* in
    BOILERPLATE_MIN_PERCENT of that repository's bodies. The first keeps a
    handful of coincidental repeats out; the second scales the bar with the
    repository, so a busy one needs far more than five.
    """
    return frozenset(
        key
        for key, seen in sorted(counts.items())
        if seen >= BOILERPLATE_MIN_COMMITS and seen * 100 >= bodies * BOILERPLATE_MIN_PERCENT
    )


def _is_trailer(line: str) -> bool:
    """Whether a line is a git trailer rather than something a person wrote.

    Judged on shape: a hyphenated key (`Former-commit-id`, `Signed-off-by`) or a
    value of one token (`Severity: minor`, a bare URL) is metadata. A plain key
    with a phrase after it - `Tests: added unit tests for the pipe` - says
    something, and stays.
    """
    probe = line.strip()
    if BOT_LINE.match(probe) or probe.lower().startswith(AUTHOR_LINE):
        # Markers the whole-body rejection reads have to survive line filtering.
        # `updated-dependencies:` is trailer-shaped, and removing it here would
        # leave the rest of a machine's body looking like prose.
        return False
    match = TRAILER_LINE.match(probe)
    if match is None:
        return False
    key, value = match.group(1), match.group(2).strip()
    if REFERENCE_KEY.match(key) or config.TICKET_PATTERN.fullmatch(key):
        return False
    return "-" in key or " " not in value


def _keep_line(line: str, boilerplate: frozenset[str]) -> bool:
    """Whether one body line is prose rather than plumbing."""
    probe = line.lstrip()
    if probe.lower().startswith(TRAILER_PREFIXES):
        return False
    if _is_trailer(probe):
        return False
    if SEPARATOR_LINE.match(probe):
        return False
    return normalise_line(line) not in boilerplate


def _rejection(cleaned: str) -> str:
    """Why what survived line filtering is still not usable prose, or "" when
    it is. The reason is reported, so an operator can see which net caught what."""
    lines = [line.lstrip() for line in cleaned.split("\n")]
    if BOT_NAME.search(cleaned) or any(BOT_LINE.match(line) for line in lines):
        return "bot-text"
    if any(line.lower().startswith(AUTHOR_LINE) for line in lines):
        return "squashed-authors"
    if sum(1 for line in lines if line.startswith(MERGE_BULLET)) >= MIN_MERGE_BULLETS:
        return "merge-list"
    return "too-short" if len(cleaned) < MIN_BODY_CHARS else ""


def _body_blocks(body: str, boilerplate: frozenset[str]) -> list[list[str]]:
    """A body's usable lines, grouped into the paragraphs its author wrote."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line:
            if current:
                blocks.append(current)
                current = []
        elif _keep_line(line, boilerplate):
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _flatten(blocks: list[list[str]]) -> str:
    return "\n".join(line for block in blocks for line in block)


def body_rejection(body: str, boilerplate: frozenset[str] = frozenset()) -> str:
    """Why a body yields no usable prose, or "" when it yields some."""
    return _rejection(_flatten(_body_blocks(body, boilerplate)))


def _usable_blocks(body: str, boilerplate: frozenset[str]) -> list[list[str]]:
    """A body's paragraphs, or none at all when the body is not prose."""
    blocks = _body_blocks(body, boilerplate)
    return [] if _rejection(_flatten(blocks)) else blocks


def clean_body(body: str, boilerplate: frozenset[str] = frozenset()) -> str:
    """Reduce a raw commit body to usable prose, or to empty when it holds none."""
    return _flatten(_usable_blocks(body, boilerplate))


def truncate(text: str, limit: int) -> str:
    """Cut to at most limit characters, at a word boundary.

    Public because two stages need it: the intent index bounds commit bodies, and
    fetch-tickets bounds tracker comments. A second copy would drift.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    if " " in head:
        head = head[: head.rindex(" ")]
    return head.rstrip()


def body_ticket_ids(body: str, boilerplate: frozenset[str] = frozenset()) -> list[str]:
    """Ticket ids a body's prose names, in first-appearance order, capped.

    First-appearance rather than set order because output has to be identical
    between runs, and set iteration order is not.
    """
    ids: list[str] = []
    for ticket in config.TICKET_PATTERN.findall(clean_body(body, boilerplate)):
        if ticket not in ids:
            ids.append(ticket)
        if len(ids) == BODY_TICKET_LIMIT:
            break
    return ids


def body_description(body: str, boilerplate: frozenset[str] = frozenset()) -> str:
    """A body's opening paragraph, truncated at a word boundary."""
    blocks = _usable_blocks(body, boilerplate)
    if not blocks:
        return ""
    return truncate(" ".join(blocks[0]), BODY_DESCRIPTION_CHARS)


def body_evidence(body: str, boilerplate: frozenset[str] = frozenset()) -> str:
    """A body's prose as its author wrote it, truncated at a word boundary.

    All of the prose, not only the opening paragraph a description falls back to,
    and cut at BODY_EVIDENCE_CHARS rather than the description length: this is
    stored as evidence in its own right, so what a person said about the change
    after their first sentence is the part worth having.
    """
    return truncate(clean_body(body, boilerplate), BODY_EVIDENCE_CHARS)


def _automated_identity(person: object) -> bool:
    """Whether one author or committer identity is a machine."""
    if not isinstance(person, dict):
        return False
    name = str(person.get("name") or "")
    email = str(person.get("email") or "")
    if BOT_ACCOUNT in name.lower() or BOT_ACCOUNT in email.lower():
        return True
    # The local part only: real contributors use NNNNNN+user@users.noreply.github.com
    # addresses, so the host is never a bot signal.
    pattern = _identity_pattern()
    if pattern is None:
        return False
    return bool(pattern.search(name) or pattern.search(email.split("@")[0]))


def _identity_label(commit: dict) -> str:
    """The author identity to name in the run report."""
    person = commit.get("author")
    if not isinstance(person, dict):
        return "unknown"
    name = str(person.get("name") or "").strip()
    email = str(person.get("email") or "").strip()
    return f"{name} <{email}>" if name or email else "unknown"


def is_automated(commit: dict) -> bool:
    """Whether a commit's body was written by a machine rather than a person.

    Author and committer are both checked: a bot's commit can be committed by a
    person's merge, and a person's commit can be committed by automation.
    """
    return any(_automated_identity(commit.get(role)) for role in ("author", "committer"))


def commit_body(commit: dict) -> str:
    """The body to read, which is nothing when a machine wrote it.

    Only the body is discarded. The commit's files, dates and any ticket its
    subject names still count, exactly as before.
    """
    if is_automated(commit):
        return ""
    return str(commit.get("body") or "")


def commit_description(subject: str, body: str, boilerplate: frozenset[str] = frozenset()) -> str:
    """The description to store: the subject's own words where they say
    something, otherwise the body's opening paragraph."""
    description = clean_description(subject)
    if len(description) >= MIN_DESCRIPTION_CHARS and not JUNK_DESCRIPTION.match(description):
        return description
    return body_description(body, boilerplate)


def ticket_pool() -> defaultdict[str, dict]:
    """The per-ticket accumulator `apply_commit` folds commits into.

    One definition, because the shape is shared between the stage and its tests
    and a pool missing a field fails only where that field is written.
    """
    return defaultdict(
        lambda: {
            "descriptions": defaultdict(int),
            "subjects": defaultdict(int),
            "bodies": defaultdict(int),
            "repos": set(),
            "first": None,
            "last": None,
            "count": 0,
        }
    )


def _pool_text(info: dict, description: str, subject: str, prose: str) -> None:
    """Add one commit's text to a ticket's pools, skipping what it does not have.

    Counted per ticket, not per commit: a commit naming two tickets is evidence
    about both, and the counts are what ranks the text within each ticket.
    """
    for pool, text in (("descriptions", description), ("subjects", subject), ("bodies", prose)):
        if text:
            info[pool][text] += 1


def stored_value(text: str, report: RedactionReport | None = None) -> str:
    """One redacted value, or "" when redaction left it saying nothing.

    Applied to each field's final text rather than to the subject or body it came
    from, because "nothing left" is a property of the stored value: cleaning
    strips a ticket prefix, so a subject of a reference and nothing else only
    becomes bare placeholder once it has been through that.
    """
    if not text or not sensitive.is_redaction_only(text):
        return text
    if report is not None:
        # Per field, because each is a value the artefact would have carried.
        report.emptied += 1
    return ""


def _widen_range(record: dict, date: str) -> None:
    """Widen a first/last date range to include this date."""
    if record["first"] is None or date < record["first"]:
        record["first"] = date
    if record["last"] is None or date > record["last"]:
        record["last"] = date


def commit_tickets(commit: dict, boilerplate: frozenset[str] = frozenset()) -> list[str]:
    """The tickets a commit names, in the order it names them.

    The body is read only when the subject names nothing, so a body mentioning
    neighbouring work cannot dilute links the subject already states.
    """
    subject_tickets = list(dict.fromkeys(config.TICKET_PATTERN.findall(commit["subject"])))
    if subject_tickets:
        return subject_tickets
    return body_ticket_ids(commit_body(commit), boilerplate)


def apply_commit(
    commit: dict,
    files: dict[str, dict],
    descriptions: dict[str, dict],
    boilerplate: frozenset[str] = frozenset(),
    redactions: RedactionReport | None = None,
) -> bool:
    """Fold one commit's tickets into the per-file entries and the per-ticket
    pools of description, subject and body text. True if ticketed.

    The three pools are independent on purpose. A description is curated - the
    subject where it says something, the body where it does not - so it can carry
    only one of the two, and the junk filter drops the weakest subjects
    altogether. The subject and body pools keep both, as written.

    The subject and the body are **redacted first**, before anything derives a
    value from them, because the length and junk filters have to judge the text
    that will actually be stored - redaction changes the length. Tickets are read
    from the unredacted commit: a ticket id is a link, not a value, and the rules
    cannot match one.

    Merge commits are skipped: a merge's file list is the whole branch, so
    indexing one would attribute hundreds of files to a single ticket.
    """
    if commit.get("is_merge"):
        return False
    tickets = commit_tickets(commit, boilerplate)
    if not tickets:
        return False
    date = commit["author_date"][:10]
    counts = None if redactions is None else redactions.by_rule
    subject_text = sensitive.redact(commit["subject"], counts)
    body = sensitive.redact(commit_body(commit), counts)
    description = stored_value(commit_description(subject_text, body, boilerplate), redactions)
    subject = stored_value(clean_description(subject_text), redactions)
    prose = stored_value(body_evidence(body, boilerplate), redactions)
    for ticket in tickets:
        info = descriptions[ticket]
        info["count"] += 1
        info["repos"].add(commit["repository"])
        _widen_range(info, date)
        _pool_text(info, description, subject, prose)
    for changed in commit.get("files", []):
        entry = files[changed["path"]]
        for ticket in tickets:
            entry["tickets"][ticket] += 1
        _widen_range(entry, date)
    return True


def _record_discarded_body(commit: dict, boilerplate: frozenset[str], report: BodyReport) -> None:
    """Note why a commit's body was discarded, so the run can report it.

    Reads the redacted body, so it classifies the same text the indexing pass
    stores. Not counted here: this pass reports on bodies, not on redaction.
    """
    raw = sensitive.redact(str(commit.get("body") or ""))
    if commit.get("is_merge") or not raw.strip():
        return
    if is_automated(commit):
        report.automated += 1
        report.automated_identities[_identity_label(commit)] += 1
        return
    if clean_body(raw, boilerplate):
        return
    if clean_body(raw):
        report.boilerplate_emptied += 1
    elif body_rejection(raw) == "bot-text":
        report.bot_text += 1


def index_repository(
    ndjson: Path,
    descriptions: dict[str, dict],
    report: BodyReport | None = None,
    redactions: RedactionReport | None = None,
) -> tuple[dict[str, dict], int]:
    """Build the file -> ticket map for one repository's dataset.

    Two passes: the first learns which body lines this repository repeats, the
    second indexes with those lines removed. Boilerplate is per repository
    because a template belongs to a team, and teams map to repositories far
    better than to a whole estate - a line that is a template in one repository
    can be a genuine one-off sentence in another.
    """
    counts, bodies = count_body_lines(ndjson)
    boilerplate = learn_boilerplate(counts, bodies)
    if report is not None:
        report.boilerplate_lines += len(boilerplate)
    files: dict[str, dict] = defaultdict(
        lambda: {"tickets": defaultdict(int), "first": None, "last": None}
    )
    commits_seen = 0
    with ndjson.open(encoding="utf-8") as source:
        for line in source:
            commit = json.loads(line)
            if report is not None:
                _record_discarded_body(commit, boilerplate, report)
            if apply_commit(commit, files, descriptions, boilerplate, redactions):
                commits_seen += 1
    finalised = {
        path: {
            "tickets": dict(sorted(data["tickets"].items(), key=lambda kv: -kv[1])),
            "first": data["first"],
            "last": data["last"],
        }
        for path, data in files.items()
    }
    return finalised, commits_seen


def _ranked(pool: dict[str, int], limit: int) -> list[str]:
    """The most repeated texts in a pool first, capped.

    Ties break on the text itself, and that is not cosmetic: two runs on the same
    inputs have to be byte-identical, and equal counts would otherwise come out in
    whatever order the pool happened to be filled in.
    """
    return [text for text, _ in sorted(pool.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def _ticket_record(info: dict) -> dict:
    """One ticket's entry in the committed artefact.

    Per ticket: the best commit-message descriptions (most repeated, then
    longest), the subjects and the body prose as their authors wrote them, plus
    dates, repos and commit count. This is the estate's own record of what each
    ticket changed - the fallback business detail while Jira titles remain an
    offline CSV import.

    `d` is always written, empty list included, because that is what consumers
    already read. `s` and `b` are omitted when there is nothing to store: most
    tickets have no body prose at all, and a field present but empty reads as
    evidence that was found rather than evidence that does not exist.
    """
    record: dict = {
        "d": [
            d
            for d, _ in sorted(info["descriptions"].items(), key=lambda kv: (-kv[1], -len(kv[0])))[
                :2
            ]
        ]
    }
    for name, texts in (
        ("s", _ranked(info["subjects"], SUBJECT_LIMIT)),
        ("b", _ranked(info["bodies"], BODY_LIMIT)),
    ):
        if texts:
            record[name] = texts
    record["first"] = info["first"]
    record["last"] = info["last"]
    record["repos"] = sorted(info["repos"])
    record["n"] = info["count"]
    return record


def summarise(
    index: dict[str, dict[str, dict]],
    commits_seen: int,
    report: BodyReport | None = None,
) -> None:
    total_files = sum(len(files) for files in index.values())
    total_tickets = len(
        {t for files in index.values() for d in files.values() for t in d["tickets"]}
    )
    size_mb = config.INTENT_INDEX_PATH.stat().st_size / 1_048_576
    bodies = report or BodyReport()
    print(
        f"Indexed {total_files:,} files across {len(index)} repos from "
        f"{commits_seen:,} ticketed commits; {total_tickets:,} distinct tickets."
    )
    print(
        f"Bodies discarded: {bodies.automated:,} by automated authorship, "
        f"{bodies.bot_text:,} by bot-generated text."
    )
    if bodies.automated_identities:
        named = ", ".join(
            f"{who} ({n:,})"
            for who, n in sorted(
                bodies.automated_identities.items(), key=lambda kv: (-kv[1], kv[0])
            )[:8]
        )
        print(f"  treated as automation: {named}")
        print("  a person in that list means KSB_AUTOMATION_IDENTITIES needs narrowing")
    print(
        f"Learned boilerplate: {bodies.boilerplate_lines:,} repeated body lines suppressed, "
        f"emptying {bodies.boilerplate_emptied:,} bodies."
    )
    print(f"Wrote {config.INTENT_INDEX_PATH} ({size_mb:.1f} MB)")


def report_redactions(redactions: RedactionReport) -> None:
    """Say how many identifiers were redacted, under which rule, and how many
    values were left with nothing.

    Always, including the nothing-redacted case: a silent filter is
    indistinguishable from an estate with nothing to redact, and the two call for
    opposite responses. A count above zero is a finding about the estate as much
    as about the store - the commit messages still carry that text, whatever the
    store now publishes.

    The identifiers themselves are never printed, here or anywhere: reporting
    them would republish what redaction just removed.
    """
    # Name the rules in force, not only what they caught. The library ships one
    # default - an email address - because every other identifier format is
    # specific to a jurisdiction or a subject domain. So "nothing matched" has two
    # very different causes: an estate with clean commit messages, or an estate
    # that never declared the formats its own references take. An operator cannot
    # tell those apart from a count, and only one of them is good news.
    in_force = ", ".join(sorted(config.SENSITIVE_PATTERNS))
    print(f"  redaction rules in force: {in_force}")
    if len(config.SENSITIVE_PATTERNS) == 1 and "email-address" in config.SENSITIVE_PATTERNS:
        print(
            "    only the shipped default - declare this estate's own reference, "
            "identifier and postal formats in KSB_SENSITIVE_PATTERNS"
        )
    if not redactions.by_rule:
        print("  redacted 0 identifiers: no mined text matched a redaction rule")
        return
    named = ", ".join(
        f"{rule} ({count:,})"
        for rule, count in sorted(redactions.by_rule.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    print(f"  redacted {sum(redactions.by_rule.values()):,} identifiers in mined text: {named}")
    print(
        f"  {redactions.emptied:,} values were left with nothing but redactions and were "
        "not stored; names are not detected, so a redacted value can still describe "
        "an identifiable person's case"
    )


def main() -> int:
    ndjson_files = sorted(config.HISTORY_DIR.glob("*/commits.ndjson"))
    if not ndjson_files:
        print(f"No history datasets under {config.HISTORY_DIR}. Run: knowledgestore export-history")
        return 1

    index: dict[str, dict[str, dict]] = {}
    descriptions: dict[str, dict] = ticket_pool()
    commits_seen = 0
    report = BodyReport()
    redactions = RedactionReport()
    for ndjson in ndjson_files:
        index[ndjson.parent.name], repo_commits = index_repository(
            ndjson, descriptions, report, redactions
        )
        commits_seen += repo_commits

    with io.gzip_text(config.INTENT_INDEX_PATH) as out:
        json.dump(index, out, ensure_ascii=False)

    ticket_out = {ticket: _ticket_record(info) for ticket, info in descriptions.items()}
    with io.gzip_text(config.TICKET_DESCRIPTIONS_PATH) as out:
        json.dump(ticket_out, out, ensure_ascii=False)
    described = sum(1 for t in ticket_out.values() if t["d"])
    subjected = sum(1 for t in ticket_out.values() if t.get("s"))
    bodied = sum(1 for t in ticket_out.values() if t.get("b"))
    print(
        f"Ticket descriptions: {len(ticket_out):,} tickets, "
        f"{described:,} with usable descriptions -> {config.TICKET_DESCRIPTIONS_PATH}"
    )
    print(
        f"  commit text kept as evidence: {subjected:,} tickets with subjects, "
        f"{bodied:,} with body prose."
    )
    report_redactions(redactions)

    summarise(index, commits_seen, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
