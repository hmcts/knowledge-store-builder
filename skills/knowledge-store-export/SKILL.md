---
name: knowledge-store-export
description: Use when a knowledge-store finding needs to leave the conversation — attached to a ticket, sent to a data-protection or security owner, or handed to a team that will act on it. Produces a dated, self-contained markdown export with evidence, provenance and honest limits, and keeps sensitive values out of it.
---

# Exporting a finding for further investigation

A store query answers a question inside a conversation. An **export** is that
answer made durable and portable: attachable to a ticket, readable by someone
who was not there, and actionable by a team that will not re-run your queries.

The difference matters. A conversational answer can be hedged in dialogue; an
export is read alone, months later, by someone deciding whether to notify a
regulator. Write for that reader.

## When to produce one

- A finding needs an owner who is not in the conversation.
- The finding will drive work: a ticket, a remediation plan, a risk decision.
- Someone asks for "a document I can attach", "something for the DPO", "a
  write-up for the team".

**Do not** produce one instead of a topic brief. They are different things:

| | Topic brief (`docs/topics/`) | Export (`exports/`) |
|---|---|---|
| Audience | anyone querying the store, forever | a named owner, now |
| Lifecycle | maintained, regenerated with the store | dated, superseded, disposable |
| Served by the explorer | yes | no |
| Committed | yes | **no** (see below) |

If the answer is durable estate knowledge people will keep asking for, it is a
topic brief. If it is "here is what we found and who should act", it is an
export.

## Where it goes, and why it is not committed

Write to `exports/YYYY-MM-DD-<subject>.md` in the store, and keep that directory
git-ignored apart from its README. An export is a derivative of the store, not
part of it, and it is often sharper than the store it came from — a document
listing exactly where sensitive data lives should not be duplicated into every
clone and baked into the browser page. The ticket is the better system of
record: it has access control, an owner and a lifecycle.

If a specific export must be kept, force-add it and say in the commit message
why holding it is safe.

## Handling sensitive content

This is the part that is easy to get wrong under pressure, because the person
asking often says "include a sample" and means "help me find these".

**Never put the sensitive values in the export.** Not secrets, not personal
data, not credentials, not internal hostnames where those are the finding.
The export is committed-adjacent, pasted into tickets, and forwarded by email;
every copy is a fresh disclosure, and an export about a leak must not become
one.

Give instead, in descending order of preference:

1. **Exact locations** — file path and line number. This is what remediation
   actually needs, and it is not itself the sensitive value.
2. **Masked shapes** — `xxxxx.xxxxxx@xxxxx.example`, `XX######X`,
   `07xxx ***xxx`. Enough to write a detection rule, not enough to identify
   anyone or authenticate as anything. Mask the domain too when the domain is
   itself part of the finding.
3. **A regeneration recipe** — the exact command that reproduces the unmasked
   list locally, with a warning not to redirect its output into the repository
   or paste it anywhere shared. The export carries the *method*; the data stays
   where it already is.

Say plainly, near the top, that the document contains no values and why. That
sentence is what stops the next person "helpfully" adding them.

When the request explicitly asks for values, do not silently comply and do not
silently refuse: explain that locations plus shapes plus a regeneration command
serve the purpose better, and that copying the data would widen the exposure the
document exists to close.

## Anything a subagent found, verify before publishing it

An export is quoted in tickets and read by people making decisions, so a
fabricated detail in one costs more than in a conversation. If any part of the
finding came from a subagent — a search, an analysis, a count — **re-derive it
yourself before it goes in the document.** Run the command again; check the
number; open the file. A subagent's report is evidence that it believes it
finished, not that it was correct, and §8's reproduction commands are the natural
place to do this: if you cannot make a command produce the number you are about
to publish, the number does not go in. See `docs/grounding-and-verification.md`.

## Register: plain, testable, no persuasion

An export is read by someone deciding whether to notify a regulator, pull a
release, or spend a sprint. Persuasive writing actively harms that: it makes the
reader discount everything, including the parts that matter. Write like a lab
report, not like a memo.

**Rules, in decreasing order of how often they are broken:**

1. **Every claim carries its measurement.** Not "a large number of records" but
   the count. Not "most of the exposure" but "N of M (P%)". If you cannot put a
   number on it, say the number is not known.
2. **Label the epistemic status.** Measured, derived, inferred, or not
   determined. A count of distinct values is measured; "these belong to real
   people" is inferred from evidence you must then give. Never let an inference
   inherit the authority of a measurement.
3. **No intensifiers.** Delete *critical, severe, alarming, significant, major,
   serious, worrying, concerning, substantial* wherever a number would do the
   work. Severity is conveyed by the figure and by what the data enables, not by
   adjectives.
4. **No editorial asides.** No *worryingly*, *strikingly*, *it is worth noting
   that*, *importantly*, *the good news is*, *this is the finding*. If a fact
   deserves prominence, give it its own section or put it first.
5. **No throat-clearing.** Start at the finding. Delete any opening that
   describes the document, the landscape, or the importance of the topic.
6. **No rhetorical structure.** No rule-of-three lists for effect, no
   dramatic dashes, no sentence fragments for emphasis, no rhetorical questions.
7. **No hedge stacking.** One qualifier, chosen deliberately: *may*, or
   *is consistent with*, or *not determined*. Not *could potentially possibly*.
8. **Prefer tables to prose for anything enumerable.** File lists, counts,
   set comparisons, open questions. Prose is for reasoning, tables are for facts.
9. **Distinguish occurrences from distinct values, always.** One value repeated
   578 times is one thing. Conflating the two is the most common way these
   documents mislead.
10. **State what would falsify the conclusion.** If the reader cannot see how
    you could be wrong, they cannot calibrate how much to trust you.

**Before finishing, run this check:**

- [ ] Every number has a denominator or a scope.
- [ ] Every inference is labelled as one, with its evidence.
- [ ] Search the draft for the words in rule 3. Each survivor is justified or cut.
- [ ] Nothing in the document is persuasion. Remove the sentence that argues.
- [ ] A reader who disagrees with the conclusion can still use the data.
- [ ] Any earlier error of yours is corrected in the text, not silently fixed.
- [ ] Reproduction commands actually run, as written, from a clean clone.

## Required structure

Use these sections, in this order. Omit one only when it is genuinely empty, and
say so rather than dropping it silently.

```markdown
# <Subject, stated as fact not question>

| Field | Value |
|---|---|
| Produced | YYYY-MM-DD |
| Produced by | <store, skill> |
| Evidence base | <graph build, sync commits> |
| Method | <one line, enough to judge coverage> |
| Contains <sensitive class> | No. <what it carries instead.> |
| Reproducible | Yes — commands in §N |

## 1. Finding
<What is true. Numbers. No preamble. If severity turns on a specific property
of the data, state that property here.>

## 2. Method
<What was scanned, what was excluded, how things were classified, and the
counting convention. Enough that someone can judge coverage and repeat it.>

## 3. <Hypothesis test, when the finding depends on one>
<When "is this real / is this actually a problem" determines the response, test
it explicitly. Independent lines of evidence, each measured. State the
conclusion, its confidence, and what would overturn it.>

## 4. Distribution
<Where it is concentrated. Set arithmetic if exclusions are being considered:
readers will ask "what if we ignore X", so answer it with numbers.>

## 5. Locations
<Tables. Path, count, classification. Precise enough to open the file.>

## 6. Lower-order observations
<What looked like a problem and is not, with the measurement that shows it.
Omitting this invites someone to "discover" it later as a new finding.>

## 7. Open questions
<Table: question, suggested owner, what it blocks. This is what turns a
document into work. Separate "not determined" from "someone must decide".>

## 8. Reproduction
<Exact commands. Warn where output is sensitive.>

## 9. Remediation options
<Ordered by exposure removed per unit of effort. Mark which need only the
owning team and which need authority. Options, not instructions.>

## 10. Not examined
<Scope limits. Non-negotiable. What the method cannot see, what was skipped,
what remains unverified.>
```

## What every export contains

**A header table**, so the document survives detachment from its conversation:
produced date, what produced it, the evidence base (graph build and sync
commits, from `knowledge/provenance.json`), the method, what the document
deliberately excludes, and suggested handling.

**A summary for the ticket** — the finding in a few sentences, with the number
that conveys severity. Lead with what is true, not with how it was found.

**The problem, in parts, ordered by severity.** Separate what is serious from
what merely looks serious: a value repeated 500 times in fixtures is not 500
problems. Distinct counts versus total occurrences is usually the distinction
that matters, and stating both is what lets a reader judge for themselves.

**Locations as tables** — file, count, and what makes it that severity. Precise
paths; a reader should be able to open the file without searching.

**Investigation tasks** — the questions the export cannot answer, each with a
suggested owner, ordered by what unblocks the most. This is the section that
turns a document into work. Distinguish "we could not determine this" from
"someone must decide this".

**Suggested remediation, prioritised**, with the cheapest self-contained win
first. Say which items have no operational justification (easy) and which are
architectural decisions (needs a person with authority).

**What this does not cover.** Non-negotiable. Scan scope, whether git history
was examined, what the method cannot detect, and what remains unconfirmed. An
export that overstates its coverage causes worse decisions than one that admits
gaps — and absence of evidence is not evidence of absence.

## Honesty rules specific to exports

- **Distinguish measured from inferred.** A count of distinct values in a file
  is measured. A conclusion about who those values belong to is inferred from
  shape, distinctness and volume — it can be a strong inference, but say which
  it is.
- **Correct your own earlier numbers in the document.** If a first pass
  misclassified something (a `__tests__` directory under `src/app/` read as
  shipped code, say), state the correction rather than quietly publishing the
  better number. The reader is calibrating how much to trust the rest.
- **Do not soften a finding to be reassuring, or sharpen it to be heard.** Both
  distort the decision the reader has to make.
- **Stamp it and expect it to go stale.** An export is true of one build. Say
  which, and say that regenerating is the way to check.
