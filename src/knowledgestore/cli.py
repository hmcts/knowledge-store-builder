"""Command line entry point: `knowledgestore <stage> [args]`.

Each stage is a module with a `main()` returning an exit code. Stages are
independent and idempotent, so you can re-run one without repeating the
others. `knowledgestore` with no arguments lists them in run order.
"""

from __future__ import annotations

import argparse
import sys

from . import config

# name -> (module attribute, one-line help). Order is the pipeline run order.
STAGES: dict[str, tuple[str, str]] = {
    "discover": (
        "generate_repository_list",
        "list the estate's repositories from GitHub into config/repositories.txt",
    ),
    "sync": ("sync_repositories", "clone or update every configured repository into repositories/"),
    "export-history": ("export_git_history", "export per-repository commit history datasets"),
    "context": (
        "build_knowledge_context",
        "write knowledge_context.md and the repository manifest",
    ),
    "intent": (
        "build_intent_index",
        "index file -> ticket links and mine ticket descriptions from commits",
    ),
    "ticket-titles": (
        "import_ticket_titles",
        "merge real ticket titles from an issue-tracker CSV export",
    ),
    "gherkin": ("extract_gherkin", "add Gherkin features, scenarios and ticket links to the graph"),
    "summaries": (
        "build_community_summaries",
        "extract community digests, or merge written summaries back in",
    ),
    "semantic": (
        "build_semantic_index",
        "build the token-neighbour map that bridges vocabulary gaps",
    ),
    "topics": (
        "build_topic_briefs",
        "extract topic evidence dossiers, or merge written briefs back in",
    ),
    "explorer": ("build_explorer", "build the self-contained explorer.html search page"),
    "status": (
        "status",
        "report provenance, layer coverage, dangling citations and page freshness",
    ),
}


def usage() -> str:
    width = max(len(name) for name in STAGES)
    lines = [
        "usage: knowledgestore <stage> [args]",
        "",
        "Stages, in pipeline run order:",
        "",
    ]
    lines += [f"  {name:<{width}}  {help_}" for name, (_, help_) in STAGES.items()]
    lines += [
        "",
        "Stages that both extract evidence and merge written prose back in",
        "(summaries, topics) take a sub-command:",
        "",
        "  knowledgestore summaries extract",
        "  knowledgestore summaries merge <written.json> [...]",
        "",
        "Global options:",
        "  --root PATH   the knowledge store directory (default: current directory)",
        "",
        "Settings can also come from the environment: KSB_ROOT, KSB_GITHUB_ORG,",
        "KSB_TICKET_BROWSE_URL, KSB_EXPLORER_TITLE and others - see config.py.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0 if argv else 1

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root")
    known, rest = parser.parse_known_args(argv)
    if known.root:
        config.configure(root=known.root)

    stage = rest[0] if rest else ""
    if stage not in STAGES:
        print(f"unknown stage: {stage or '(none)'}\n", file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 1

    module_name = STAGES[stage][0]
    module = __import__(f"{__package__}.{module_name}", fromlist=["main"])
    # Stage mains read sys.argv for their own sub-commands and arguments.
    sys.argv = [f"knowledgestore {stage}", *rest[1:]]
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
