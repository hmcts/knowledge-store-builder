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

const GUARDS = [unexcludedClean];

export function decide({ command, cwd = "", state = {} } = {}) {
  for (const segment of chainSegments(command)) {
    for (const guard of GUARDS) {
      const deny = guard(segment, cwd, state, command);
      if (deny) return { allow: false, deny };
    }
  }
  return { allow: true };
}
