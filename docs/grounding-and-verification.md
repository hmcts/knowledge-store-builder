# Grounding and verification

Everything a knowledge store says must trace to something in the store. This
document states that contract, and says how to check it — including the case
that is easy to miss, where the prose was written by a subagent and the
dispatching agent checked that it *arrived* rather than that it was *true*.

> **This document is the master for these rules, and they are mirrored inside
> the skills.** If you change anything here, update every copy in the same
> change — an agent reads the skill it was invoked with and may never open this
> file, so a superseded rule in a skill is the rule that gets applied.
>
> | Mirrored in | Which part |
> |---|---|
> | `skills/knowledge-store/SKILL.md` — honesty rules | traceability of every claim; say which layer answered |
> | `skills/knowledge-store-build/SKILL.md` — before merging | verify grounding, not only coverage; the dispatcher verifies and cannot delegate; run `summaries verify` |
> | `skills/knowledge-store-export/SKILL.md` — before publishing | re-derive anything a subagent found |
> | `docs/building-a-knowledge-store.md` — keeping a store honest | one-line pointer |

## The contract

**Every claim in a store, and every claim in an answer from a store, traces to
evidence in the store.** Node names, source paths, repository names, ticket ids,
schema field names, commit subjects and bodies. Nothing else is admissible.

Three things follow.

**Interpretation is allowed; invention is not.** A boolean field named
`requiresInterpreter` supports "this service records whether a person needs an
interpreter". It does not support "this service assigns interpreters to
hearings" unless something in the evidence says so. The line is whether a reader
could check your claim against the same evidence and agree.

**Absence of evidence is a finding, and must be reported as one.** "The graph
holds no cross-repository call edges, so this cannot be determined" is a correct,
useful answer. Filling that gap from general knowledge of how systems usually
work is the failure this document exists to prevent — it is indistinguishable
from a correct answer to the reader, and wrong.

**Say which layer answered.** A committed topic brief, a community summary, raw
graph nodes and commit history have different reliability. Prose was written at a
point in time against a specific build; nodes are mechanical. When they
disagree, the mechanical layer wins and the prose is stale.

## Why subagents need explicit verification

Authoring and extraction are fanned out across subagents: one per digest batch,
one per extraction chunk. Each returns text or JSON that goes into the store.

**A subagent's report is not evidence that its work is correct.** It is evidence
that the subagent believes it finished. Those are different claims, and only one
of them is checkable from the report.

The failure mode is specific and quiet. A subagent given 45 community digests
will return 45 summaries. They will be the right length, the ids will all match,
the merge will accept them, and the coverage check will pass — **and any number
of them may describe behaviour the digest does not show.** Every mechanical gate
in the pipeline passes, because every mechanical gate measures shape, not truth.

This has a second edge: the subagent that fabricates is not being careless. It is
doing what generative models do when evidence is thin — producing the most
plausible continuation. Thin evidence is exactly what a small or label-less
cluster provides. So invention concentrates precisely where verification is
hardest and the reader is least able to spot it.

## The rule

**The dispatching agent verifies subagent output before it enters the store. It
is not delegable to the subagent that produced it.**

Self-verification by the author catches format errors and nothing else. A
subagent asked "did you follow the rules?" will say yes, and will be reporting
its belief, not a check.

Concretely, when dispatching subagents that produce store content:

1. **Instruct grounding in the dispatch**, in the subagent's own terms: base
   every claim only on the supplied evidence; interpreting what a name implies
   is fine; inventing behaviour the names do not show is not; say "not
   determinable from this digest" rather than filling a gap.
2. **Verify coverage** — every id present, nothing extra, lengths in range. This
   is necessary and not sufficient.
3. **Verify grounding** — see the techniques below. Necessary, and the step that
   is usually skipped.
4. **Reconcile counts** — what the agents wrote against what the merge accepted.
   A shortfall is a defect somewhere, and "N merged" alone does not reveal it.
5. **Record what was verified and how**, so the next person knows what the
   coverage figure does and does not imply.

## Verification techniques, cheapest first

**Mechanical citation check (automatable, strongest).** Every proper noun in a
generated summary — repository name, file path, class or field identifier,
ticket id — must appear in the digest that produced it. Extract the identifiers
from the prose and set-difference them against the evidence. Anything in the
prose that is not in the evidence is either a fabrication or a paraphrase worth
inspecting. This catches invented class names and misattributed repositories,
which are the most common and most damaging errors, and it scales to thousands of
summaries at no model cost.

**Vocabulary check (automatable).** Speculation words in a layer that should be
factual — *probably*, *likely*, *appears to*, *presumably*, *suggests that* —
mark places where the author had no evidence and continued anyway. A layer of
factual descriptions should contain almost none.

**Sampling with source comparison (cheap, catches systematic drift).** Draw a
random sample — 20 or 30 is enough to detect a systematic problem across
thousands — and read each summary against its digest, claim by claim. Sampling
detects a bad prompt, a misunderstood convention, or a model drifting into
narrative. It will not find one bad summary in five thousand, and does not claim
to.

**Adversarial re-read (model cost, highest fidelity).** Give a second agent the
evidence and the prose, and ask it to list claims the evidence does not support.
Frame it to find fault: an agent asked "is this correct?" tends to agree, and one
asked "what here is unsupported?" tends to look. Worth reserving for the layers
people quote most — topic briefs, deep dives — rather than every summary.

**Bite-check the checker.** A verification that passes against deliberately
broken content is not verification. Corrupt one summary — swap in a class name
from a different repository — and confirm the check fails. An unbitten check is
decoration, and worse than none because it produces false confidence.

## What to tell the reader

State what was verified and how, in the store rather than in a conversation that
disappears:

- which layers are mechanically derived and which are LLM-authored
- what verification each authored layer received, and at what sample rate
- that authored prose is true of a specific build and goes stale silently

A reader who knows a layer was coverage-checked but not grounding-checked can
calibrate. A reader told only "N summaries, full coverage" will reasonably assume
more than that number supports.

## The shipped check

```bash
knowledgestore summaries verify [--sample N] [--strict]
```

Both automatable checks above are implemented: identifiers cited in each summary
are compared against those its digest contains, and speculation words are
flagged. It reports by default so it can be run over a whole store without
blocking; `--strict` exits non-zero for CI; `--sample N` takes a deterministic
subset and prints its own rate, so the output cannot be read as full coverage.

**What it will and will not tell you.** A finding means the prose cites an
identifier the evidence does not contain. That is usually one of three things,
and only the first is fabrication:

- a name that does not exist, sometimes a blend of two that do
- interpretation the reader might accept (naming a class when the evidence shows
  only its test)
- a spelling difference the checker has not been taught

Comparison is on letters and digits only, so method decoration (`.saveDecision()`
against `saveDecision`) and this estate's kebab-schema/CamelCase-class convention
both count as grounded, while a longer or different name still differs. English
compound adjectives ("police-to-courtroom") are excluded from identifier
detection, because flagging them trains readers to ignore the report.

**Calibrating on a real store.** Tuned against ~5,300 authored summaries:
summaries written directly against their own digest flagged at 9%, while
summaries carried across a re-cluster by `summaries remap` flagged at 37%. Two
things follow. The four-fold gap is the check working — remapped prose cites the
evidence of the cluster it was written for, not the one it now sits on. And a
remap preserves *coverage* while degrading *grounding*, which is worth knowing
before treating a high retention figure as a clean result.

Neither residual figure is all fabrication; both include interpretation and
spellings the checker has not been taught. Use it to find the worst cases and to
watch the rate between refreshes, not as a defect count.
