"""Cut the communities without prose into authoring batches, and gate what comes back.

Two subcommands of `knowledgestore summaries`, kept out of
`build_community_summaries` because that module is already the largest in the
library. Its dispatcher parses their flags, so the documented-flags gate still
reads every flag the stage accepts from the one file it knows to look in.

  knowledgestore summaries batches --out-dir DIR [--size 100]
      One batch file per `--size` significant communities that have no prose, as
      `{"batch": N, "out": "<DIR>/out/summaries_NN.json", "digests": [...]}`, with
      `out` absolute. The
      authoring agent reads the digests and writes `{"<id>": "<summary>"}` to `out`;
      that `out` file is what `summaries merge` takes.

  knowledgestore summaries check-batch BATCH.json [...]
      The authoring brief's mechanical gate, so no agent writes its own. Reads each
      batch's `out` and prints every violation; exit 1 on any, or when it checked
      nothing.

Reuse, deliberately, rather than a second copy: the length bounds `merge` enforces
and the grounding rule `summaries verify` applies (`_ungrounded`, private to that
module and imported here the way `check_citations` imports `_normalise`). A gate
that disagreed with `merge` about length, or with `verify` about grounding, would
pass prose the next stage rejects - or fail prose it accepts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from . import io
from .build_community_summaries import MAX_SUMMARY_LEN, MIN_SUMMARY_LEN, _ungrounded

# The brief asks for 2-4 sentences. A sentence ends at `.`, `!` or `?` followed
# by whitespace and a capital, which is what keeps `pom.xml` and `v1.2` whole.
MIN_SENTENCES = 2
MAX_SENTENCES = 4
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

BATCH_PREFIX = "batch_"


def sentence_count(text: str) -> int:
    """How many sentences `text` holds, by the break rule above."""
    return len([part for part in _SENTENCE_BREAK.split(text.strip()) if part])


def unwritten_digests() -> list[dict]:
    """The significant communities with no prose, in the digests' own order."""
    digests = io.read_json(config.SUMMARIES_INPUT_PATH) or []
    have = set(io.read_summaries(config.SUMMARIES_PATH))
    return [digest for digest in digests if str(digest["id"]) not in have]


def write_batches(out_dir: Path, size: int) -> int:
    """Write the batch files; refuse, writing nothing, over an earlier run's.

    Numbers are zero-padded to the width of the largest, so the files sort in batch
    order. An earlier run's batches in the same directory are refused rather than
    overwritten: a run cutting fewer batches than the last would leave the higher
    numbers behind, still valid, describing communities that may since have prose.
    """
    if size < 1:
        print(f"--size must be at least 1, not {size}")
        return 1
    if not config.SUMMARIES_INPUT_PATH.exists():
        print(f"no digests at {config.SUMMARIES_INPUT_PATH} - run `summaries extract` first")
        return 1
    earlier = sorted(out_dir.glob(f"{BATCH_PREFIX}*.json")) if out_dir.is_dir() else []
    if earlier:
        print(
            f"refused: {out_dir} already holds {len(earlier)} batch file(s) from an earlier "
            "run; nothing was written. Choose an empty directory."
        )
        return 1
    todo = unwritten_digests()
    chunks = [todo[start : start + size] for start in range(0, len(todo), size)]
    width = max(2, len(str(len(chunks))))
    for number, chunk in enumerate(chunks, start=1):
        # Absolute, so an agent working from another directory writes where the
        # gate and merge will look. `absolute`, not `resolve`: resolving collapses
        # `..`, which the write guard exists to see.
        out = out_dir.absolute() / "out" / f"summaries_{number:0{width}d}.json"
        batch = {"batch": number, "out": str(out), "digests": chunk}
        io.write_json(out_dir / f"{BATCH_PREFIX}{number:0{width}d}.json", batch)
    if chunks:
        # The one write here that does not go through `io.write_json`, so it is
        # validated by the same guard before it runs.
        io.checked_write_target(out_dir / "out")
        (out_dir / "out").mkdir(parents=True, exist_ok=True)  # NOSONAR(S2083) - validated above
    print(
        f"{len(todo)} significant communities without prose -> {len(chunks)} batch(es) "
        f"of up to {size} in {out_dir}"
    )
    return 0


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    """A JSON object hook that refuses a repeated key instead of keeping the last."""
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"id {key!r} appears more than once")
        seen[key] = value
    return seen


def _read_batch(path: str) -> tuple[list[dict], str, dict]:
    """The batch's digests, its `out` path, and the authored object read from it."""
    batch = io.read_json(Path(path))
    if batch is None:
        raise FileNotFoundError(f"no batch file at {path}")
    digests, out = batch["digests"], str(batch["out"])
    # NOSONAR(S2083, S8707) - reading the `out` path the operator's batch names is
    # the purpose of check-batch. Grounds are stated once in
    # `build_community_summaries.merge`; this site cites them rather than restating
    # them, so the two cannot drift. Read here rather than through `io` because the
    # duplicate-id rule needs the object hook.
    text = Path(out).read_text(encoding="utf-8")  # NOSONAR(S2083, S8707)
    authored = json.loads(text, object_pairs_hook=_unique_keys)
    if not isinstance(authored, dict):
        raise TypeError(f"{out} must be one object of id -> summary")
    return digests, out, authored


def _summary_violations(cid: str, summary: object, digest: dict) -> list[str]:
    """Every rule one summary breaks."""
    if not isinstance(summary, str):
        return [f"MISSING {cid}: no summary"]
    text = " ".join(summary.split())
    found = []
    if not MIN_SUMMARY_LEN <= len(text) <= MAX_SUMMARY_LEN:
        found.append(
            f"LENGTH {cid}: {len(text)} characters, outside {MIN_SUMMARY_LEN}-{MAX_SUMMARY_LEN}"
        )
    sentences = sentence_count(text)
    if not MIN_SENTENCES <= sentences <= MAX_SENTENCES:
        found.append(f"SENTENCES {cid}: {sentences}, outside {MIN_SENTENCES}-{MAX_SENTENCES}")
    ungrounded = sorted(_ungrounded(text, digest))
    if ungrounded:
        found.append(f"UNGROUNDED {cid}: {', '.join(ungrounded)}")
    return found


def check_batch(path: str) -> tuple[list[str], int]:
    """The violations in one batch's authored output, and how many ids it holds."""
    try:
        digests, _, authored = _read_batch(path)
        by_id = {str(digest["id"]): digest for digest in digests}
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"PARSE {path}: {error}"], 0
    found = [f"EXTRA_ID {cid}: not in {path}" for cid in sorted(set(authored) - set(by_id))]
    for cid, digest in by_id.items():
        found += _summary_violations(cid, authored.get(cid), digest)
    return found, len(by_id)


def check(paths: list[str]) -> int:
    """Check every batch, print every violation, and exit 1 on any or on nothing."""
    violations, total = [], 0
    for path in paths:
        found, count = check_batch(path)
        violations += found
        total += count
    for line in violations:
        print(line)
    print(f"checked {total} summaries over {len(paths)} batch(es); violations={len(violations)}")
    if total == 0 and not violations:
        print("nothing was checked - a batch with no digests is not a passing batch")
    return 1 if violations or total == 0 else 0
