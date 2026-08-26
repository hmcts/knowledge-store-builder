"""Command line entry point: `knowledgestore <stage> [args]`.

Each stage is a module with a `main()` returning an exit code. Stages are
independent and idempotent, so you can re-run one without repeating the
others. `knowledgestore` with no arguments lists them in run order.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__

from . import config

# Stages that build their own argument parser, and so document themselves better
# than this module can. Everything else has --help answered centrally, in main().
# A stage belongs here only once it genuinely parses arguments; listing one that
# does not would hand --help back to a stage that ignores it and runs instead.
SELF_PARSING = frozenset(
    {
        "discover",
        "export-history",
        "fetch-tickets",
        "summaries",
        "status",
        "check-evidence",
        "check-answers",
        "check-corpus",
        "chunk-plan",
        "chunk-status",
        "content-set",
        "merge-chunks",
        "merge-inputs",
        "merge-layers",
        "record-clustering",
        "dangling-endpoints",
        "extract-ast",
        "gaps",
    }
)

# name -> (module attribute, one-line help). Order is the pipeline run order.
STAGES: dict[str, tuple[str, str]] = {
    "discover": (
        "generate_repository_list",
        "list the estate's repositories from GitHub into config/repositories.txt",
    ),
    "sync": ("sync_repositories", "clone or update every configured repository into repositories/"),
    "convert": (
        "convert_documents",
        "convert Office documents to Markdown so extraction can read them",
    ),
    "extract-ast": (
        "extract_ast",
        "extract the AST layer one repository at a time (needs the `ast` extra)",
    ),
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
    "fetch-tickets": (
        "fetch_tickets",
        "ask the issue tracker about discovered tickets (opt-in; needs credentials)",
    ),
    "merge-inputs": (
        "merge_inputs",
        "name the graphs `graphify merge-graphs` would read, and reconcile them",
    ),
    "chunk-plan": (
        "build_chunk_plan",
        "write the semantic fan-out's chunk plan (run before dispatching extraction)",
    ),
    "content-set": (
        "build_content_set",
        "expose the content set a corpus search should read instead of the raw tree",
    ),
    "chunk-status": (
        "chunk_status",
        "report fan-out progress from the extractions on disk, never-launched chunks first",
    ),
    "merge-chunks": (
        "merge_chunks",
        "merge per-chunk semantic extractions without fusing unrelated entities",
    ),
    "merge-layers": (
        "merge_layers",
        "merge the AST and semantic layers without re-pointing edges at unrelated nodes",
    ),
    "gherkin": ("extract_gherkin", "add Gherkin features, scenarios and ticket links to the graph"),
    "packages": (
        "build_package_edges",
        "add cross-repository package nodes and import edges (npm layer)",
    ),
    "deployments": (
        "build_deployments",
        "add per-environment deployment config and join it to services (opt-in)",
    ),
    "record-clustering": (
        "record_clustering",
        "record which partitioner produced the graph's communities (run after clustering)",
    ),
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
    "deepdive": (
        "build_deep_dives",
        "extract a repository evidence bundle, or merge written deep dives",
    ),
    "explorer": ("build_explorer", "build the self-contained explorer.html search page"),
    "status": (
        "status",
        "report provenance, layer coverage, dangling citations and page freshness",
    ),
    "dangling-endpoints": (
        "measure_dangling_endpoints",
        "measure how many dangling edge endpoints name a node the graph already holds",
    ),
    "gaps": (
        "report_ingestion_gaps",
        "rank what this estate depends on and does not hold (a report, not an action)",
    ),
    "check-install-docs": (
        "check_install_docs",
        "check the documented install commands against what the lock file declares",
    ),
    "check-corpus": (
        "check_corpus_config",
        "report harness configuration the corpus carries (run after sync)",
    ),
    "check-evidence": (
        "check_evidence",
        "fail if committed commit-mined text identifies a specific case or person",
    ),
    "check-answers": (
        "check_answers",
        "fail if the store stopped answering its declared questions (config/questions.txt)",
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
        "(summaries, topics, deepdive) take a sub-command:",
        "",
        "  knowledgestore summaries extract",
        "  knowledgestore summaries merge <written.json> [...]",
        "",
        "Global options:",
        "  --root PATH   the knowledge store directory (default: current directory)",
        "  --version     the installed library version, for checking against the skills",
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

    if rest[:1] in (["--version"], ["-V"]):
        # Askable, because it was not. The skills are distributed separately from
        # this library - skills through the plugin cache, the library through pip -
        # so they drift, and the first question when a documented stage is
        # "unknown" is which version is actually installed. There was no way to
        # find out short of importlib.metadata.
        print(f"knowledgestore {__version__}")
        return 0

    stage = rest[0] if rest else ""
    if stage not in STAGES:
        print(
            f"unknown stage: {stage or '(none)'} (this is knowledgestore {__version__}; the "
            "skills describing a stage you do not have is the usual cause)\n",
            file=sys.stderr,
        )
        print(usage(), file=sys.stderr)
        return 1

    # Asking a subcommand what it does must never be the thing that does it.
    # Most stages parse no arguments at all, so an unhandled --help fell through
    # to the stage's default action - and for `sync` that fetches and resets every
    # repository in the estate, which is the opposite of what someone probing an
    # unfamiliar subcommand expects. Handled here rather than in each stage so a
    # stage added later inherits it instead of having to remember.
    if any(arg in ("-h", "--help") for arg in rest[1:]) and stage not in SELF_PARSING:
        print(f"knowledgestore {stage}\n\n  {STAGES[stage][1]}\n")
        print("This stage takes no arguments of its own.")
        print("Run `knowledgestore` for the full stage list, and see config.py for settings.")
        return 0

    module_name = STAGES[stage][0]
    module = __import__(f"{__package__}.{module_name}", fromlist=["main"])
    # Stage mains read sys.argv for their own sub-commands and arguments.
    sys.argv = [f"knowledgestore {stage}", *rest[1:]]
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
