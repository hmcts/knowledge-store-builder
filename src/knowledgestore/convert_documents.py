"""Convert Office documents to Markdown so extraction can read them.

graphify detects `.pdf` and a wide spread of code and markup, and is blind to
Office formats. A repository carrying a design document, a field mapping or an
interface specification contributes its filename and nothing else - and those
are exactly the documents a knowledge store exists to answer questions from.

Measured on two estates: 805 Office files and 443 CSVs on one, 503 and 253 on
the other, with 1,036 of one estate's 1,248 outside any test tree. What they are
matters more than the count - an overarching architecture HLD, a request and
response field mapping, knowledge-transfer notes.

Two design constraints, both learned the hard way elsewhere in this pipeline:

**The Markdown is written beside its source, inside the clone.** Anywhere else
and `source_file` stops being repository-relative, which silently kills the
file-to-ticket join - the index is keyed on repo-relative paths, and a converted
document filed under a different root joins nothing while every count stays
healthy.

**It is therefore destroyed by the next `sync`**, which ends with
`git clean -fd -e graphify-out`. That is deliberate rather than unfortunate:
conversion is cheap and idempotent, so regenerating after each sync is cheaper
than the alternative of a durable side-directory that breaks the join. Run this
after `sync` and before extraction.

Requires the `documents` extra:

    pip install 'hmcts-knowledge-store-builder[documents]'
    knowledgestore convert
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import config

# What markitdown can turn into Markdown that graphify cannot read at all.
# `.pdf` is deliberately absent: graphify already detects it, and converting it
# here would produce two nodes for one document.
CONVERTIBLE = (".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".csv")

# Trees whose documents are fixtures rather than knowledge. A spreadsheet under
# `src/test/resources/expected/` is an assertion, not an interface specification,
# and indexing it adds mass without adding an answer.
FIXTURE_PARTS = ("test", "tests", "target", "test-classes", "node_modules", ".git", "wiremock")

# Source sets name their test trees by convention rather than by one word:
# `integrationTest`, `functional_tests`, `e2e-test`. Matched by pattern so those
# are caught, and anchored so ordinary words that merely end in "test" - `latest`
# most obviously - are not.
FIXTURE_PATTERN = re.compile(
    r"^(unit|integration|functional|acceptance|smoke|e2e|contract|component|api|perf)"
    r"[-_]?tests?$",
    re.IGNORECASE,
)

SUFFIX = ".converted.md"


def is_fixture(path: Path) -> bool:
    return any(part in FIXTURE_PARTS or FIXTURE_PATTERN.match(part) for part in path.parts)


def convertible_documents(corpus: Path) -> list[Path]:
    """Office documents worth converting, newest-first by repository order."""
    if not corpus.is_dir():
        return []
    return sorted(
        path
        for path in corpus.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() in CONVERTIBLE
        and not is_fixture(path)
    )


def converted_path(source: Path) -> Path:
    """Where a document's Markdown goes: beside it, so the path stays repo-relative."""
    return source.with_name(source.name + SUFFIX)


def convert(source: Path, converter) -> bool:
    """Write `source` as Markdown beside itself. False when it could not be read.

    A document that fails to convert is reported rather than raised: one
    unreadable spreadsheet in a large estate must not cost the whole run, and a
    corrupt file is a fact about the corpus worth reporting once.
    """
    try:
        text = converter.convert(str(source)).text_content
    except Exception as error:  # markitdown raises a wide range per format
        print(f"  {source.name}: not converted - {error}", file=sys.stderr)
        return False
    if not (text or "").strip():
        return False
    converted_path(source).write_text(text, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    try:
        from markitdown import MarkItDown
    except ImportError:
        print(
            "The `documents` extra is not installed, so Office documents stay invisible "
            "to extraction. Install with `pip install "
            "'hmcts-knowledge-store-builder[documents]'` and re-run.",
            file=sys.stderr,
        )
        return 1

    documents = convertible_documents(config.REPOSITORIES_DIR)
    if not documents:
        print("No convertible Office documents found in the corpus.")
        return 0

    converter = MarkItDown()
    written = sum(1 for document in documents if convert(document, converter))
    print(
        f"Converted {written} of {len(documents)} Office document(s) to Markdown beside "
        f"their sources in {config.REPOSITORIES_DIR}."
    )
    if written:
        print(
            "  These are untracked, so the next `sync` deletes them - re-run this stage "
            "after every sync and before extraction."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
