# Vision

`knowledge-store-builder` exists so that a question about a large software
estate can be answered from evidence, by someone who was not there when the
code was written.

It serves the engineer, analyst or product owner who has inherited an estate
nobody holds in their head: dozens of repositories, years of commits, a tracker
full of tickets whose links to the code were never recorded. It turns that into
a set of committed files that answer questions and cite what they answered
from.

It owns exactly one thing: **the layer between a code estate and a grounded
answer about it.**

## Answers are built, not computed

A store is a set of static files committed beside the code. The work happens
once, at build time, where it can be reviewed, diffed and re-run. Reading a
store needs no server, no index to warm, no network and no model.

That is the trade this library makes. A query-time system can answer a question
nobody anticipated; it cannot be reviewed before it answers, and it cannot tell
you tomorrow what it told you today. A built store can be read in a pull
request, pinned to a commit, and handed to someone with no licence for anything.

Two consequences the library holds to:

- **Two builds from the same inputs are byte-identical.** Anything ordered by a
  set or an unordered mapping gets an explicit tiebreak. A store that churns
  cannot be diffed, and a store that cannot be diffed cannot be reviewed.
- **A stage's output is data in somebody else's repository.** Changing what a
  stage emits changes their committed files, so it is announced as a change to
  their data rather than described as a refactor.

## What is not known is reported, not smoothed over

The expensive failure in this domain is not a wrong answer. It is a confident
one. A reader cannot tell a researched conclusion from a plausible sentence, so
the library's job is to make the difference visible rather than to sound sure.

So absence of evidence is a finding. Where two applications share a name and no
edge connects them, the store reports two independent implementations rather
than guessing they are the same. Where a repository was never ingested, "no
component implements this" is a fact about what the store holds, not about the
platform.

The same rule governs the library's own checks. A check that read nothing
reports that it read nothing, rather than reporting no problems.

## It reports; a person decides

Drift, coverage gaps and stale snapshots are normal operating conditions of a
live estate. The library's stages describe what they found and exit zero. They
do not refuse the build, re-cluster a graph, or repair an estate on their own
judgement.

Where a decision needs authority the library does not have — whether a finding
may be published, whether a credential may be used, whether a requirement is
still wanted — the library states what it knows and stops.

## What it will never be

- **An extractor.** [graphify](https://github.com/safishamsi/graphify) parses
  the code and owns that. This library prepares its inputs and enriches its
  output, and re-implementing extraction here would fork a problem somebody
  else is already solving.
- **A service.** No daemon, no query-time index, no endpoint. If an answer
  needs a process running to exist, it is out of scope.
- **A model or a harness.** The explorer answers with no LLM at all. Where
  prose is generated it is generated once, at build time, from graph evidence,
  and reviewed before it is committed.
- **An owner of anyone's estate.** This repository is public and reusable.
  No consuming estate's repository names, field names, counts or findings
  belong in it — illustrations use invented names. A store's content stays in
  the store that produced it.
- **A collaboration tool.** It builds the artefact teams discuss; it does not
  host the discussion.

## How to use this document

It settles scope questions, which are the ones that cost most to get wrong
late. A proposal that gives the library a second owner of extraction, a running
process, or an opinion it has no authority for is out of scope however well it
is built. A proposal that makes an answer more traceable, a build more
reproducible, or an absence more visible is in scope even when it is dull.

Where this document and the code disagree, that is a defect in one of them.
Say which.
