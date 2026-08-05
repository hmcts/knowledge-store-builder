"""Gate a committed evidence artefact: does it identify a specific case?

The `intent` stage withholds such text as it mines, but that only helps the next
refresh. A store's artefact is already in version control and already embedded
in a published page, so there has to be a way to check what is *there* - and to
fail a build over it, which is why this is a stage of its own rather than a flag
on `status`. `status` never returns non-zero by design: drift and coverage gaps
are normal operating conditions there, the stage reports and humans decide. A
gate has the opposite contract, and the two cannot share an entry point.

**The matched value is never printed.** The finding names the ticket, the field
and the rule, which is enough to find the value in the artefact and enough to
decide what to do. Printing the text would copy it into a build log - wider read
and longer kept than the artefact - so a gate that did that would republish
exactly what it was called to protect.

A clean result is not a certificate. The rules match identifier shapes, not
personal data in general, and personal names are deliberately not detected -
`sensitive.py` says why. Read this as "nothing matched the rules", never as
"this file holds no personal data".

Run: knowledgestore check-evidence [artefact.json.gz ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, io, sensitive


def check(path: Path) -> tuple[int, int]:
    """Report on one artefact; return (values checked, findings printed)."""
    records = io.read_gzip_json_dict(path)
    if not records:
        print(f"No ticket records at {path} - nothing to check")
        return 0, 0
    checked = sum(1 for _ in sensitive.mined_values(records))
    found = sensitive.findings(records)
    if not found:
        print(f"{path.name}: {checked:,} mined values checked, none matches a withholding rule")
        return checked, 0
    # Findings go to stderr, with the advice that follows them, so a failing run
    # reads in order on one stream.
    print(
        f"{path.name}: {len(found):,} of {checked:,} mined values match a withholding rule:",
        file=sys.stderr,
    )
    for ticket, field, rule in found:
        print(f"  {ticket}  field {field}  rule {rule}", file=sys.stderr)
    return checked, len(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="ticket-description artefacts to check (default: this store's own)",
    )
    arguments = parser.parse_args(argv)
    paths = [Path(p) for p in arguments.paths] or [config.TICKET_DESCRIPTIONS_PATH]

    findings = sum(check(path)[1] for path in paths)
    if not findings:
        return 0
    print(
        "\nThe values themselves are deliberately not printed. Re-run"
        "\n`knowledgestore intent` to rebuild the artefact with them withheld,"
        "\nthen `knowledgestore explorer` so the page stops embedding them.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
