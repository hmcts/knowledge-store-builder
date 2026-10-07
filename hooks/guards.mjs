// Refuses command shapes this library has documented as destructive. Pure:
// it reads nothing, writes nothing and calls nothing outside its arguments.

/** Split a command into its &&, || and ; separated segments. */
export function chainSegments(command) {
  return String(command ?? "")
    .split(/\s*(?:&&|\|\||;)\s*/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** The first command of a segment's pipeline. */
export function pipelineHead(segment) {
  return String(segment ?? "").split(/\s*\|\s*/)[0].trim();
}

/** A segment's whitespace-separated tokens. Quoting is not honoured, which
 *  can only cause a guard to stay silent, never to fire wrongly. */
export function argsOf(segment) {
  const t = String(segment ?? "").trim();
  return t ? t.split(/\s+/) : [];
}

const EXCLUDES_GRAPHIFY_OUT = /(?:-e|--exclude)[=\s]+graphify-out\b/;

function unexcludedClean(segment, cwd) {
  const args = argsOf(segment);
  if (args[0] !== "git" || !args.includes("clean")) return null;
  const flags = args.filter((a) => /^-[^-]/.test(a)).join("");
  if (!(flags.includes("f") && flags.includes("d"))) return null;
  if (EXCLUDES_GRAPHIFY_OUT.test(segment)) return null;
  const inClone = /(^|\/)repositories\//.test(cwd) || /(^|\s)repositories\//.test(segment);
  if (!inClone) return null;
  return (
    "This clean would delete the per-repo graphs. They live untracked at " +
    "repositories/<name>/graphify-out/, and a clean without the exclusion " +
    "forces a full re-extraction; it has destroyed 61 of 81 graphs here once. " +
    "Run: git clean -fd -e graphify-out"
  );
}

function indiscriminateStage(segment) {
  const args = argsOf(segment);
  if (args[0] !== "git" || args[1] !== "add") return null;
  const rest = args.slice(2);
  if (!rest.some((a) => a === "-A" || a === "--all" || a === ".")) return null;
  return (
    "Stage explicit paths. The working tree here routinely holds generated " +
    "pipeline output, another session's edits and scratch files at once, and " +
    "-A cannot tell them apart. Read git status --short, then name the paths."
  );
}

const EXTRACTION_VERBS = new Set(["update", "extract"]);

function outsideExtraction(segment) {
  const args = argsOf(segment);
  // Only the extraction verbs. merge-graphs is given repositories/*/... by the
  // build skill itself, so matching every graphify subcommand would refuse the
  // documented merge.
  if (args[0] !== "graphify" || !EXTRACTION_VERBS.has(args[1])) return null;
  if (!args.slice(2).some((a) => /^repositories\//.test(a))) return null;
  return (
    "Extract from inside the repository. A path like repositories/<name> " +
    "prefixes every source_file with it, which breaks the file-to-ticket join " +
    "silently - the only symptom is that nodes lose their tickets. " +
    "Run: ( cd repositories/<name> && graphify update . )"
  );
}

const GUARDS = [unexcludedClean, indiscriminateStage, outsideExtraction];

export function decide({ command, cwd = "", state = {} } = {}) {
  for (const segment of chainSegments(command)) {
    for (const guard of GUARDS) {
      const deny = guard(segment, cwd, state, command);
      if (deny) return { allow: false, deny };
    }
  }
  return { allow: true };
}
