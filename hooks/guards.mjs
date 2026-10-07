// Refuses command shapes this library has documented as destructive. Pure:
// it reads nothing, writes nothing and calls nothing outside its arguments.

/** Blank the contents of quoted spans, keeping the quotes and the length, so
 *  a separator or a flag inside a string cannot manufacture a segment or a
 *  token. This is what makes the note on argsOf true: masking can only stop a
 *  guard firing, never cause one to fire. */
/** @param {unknown} command @returns {string} */
export function maskQuoted(command) {
  let out = "";
  let quote = null;
  for (const ch of String(command ?? "")) {
    if (quote) {
      out += ch === quote ? ch : "X";
      if (ch === quote) quote = null;
    } else if (ch === "'" || ch === '"') {
      quote = ch;
      out += ch;
    } else {
      out += ch;
    }
  }
  return out;
}

/** Segments with the separator that precedes each one, over masked text. */
/** @param {unknown} command @returns {{ text: string, before: string|null }[]} */
export function chainLinks(command) {
  const masked = maskQuoted(command);
  const links = [];
  const separator = /\s*(&&|\|\||;)\s*/g;
  let last = 0;
  let before = null;
  let match;
  while ((match = separator.exec(masked)) !== null) {
    links.push({ text: masked.slice(last, match.index).trim(), before });
    before = match[1];
    last = match.index + match[0].length;
  }
  links.push({ text: masked.slice(last).trim(), before });
  return links.filter((link) => link.text);
}

/** Split a command into its &&, || and ; separated segments. */
/** @param {unknown} command @returns {string[]} */
export function chainSegments(command) {
  return chainLinks(command).map((link) => link.text);
}

/** The first command of a segment's pipeline. */
/** @param {unknown} segment @returns {string} */
export function pipelineHead(segment) {
  return String(segment ?? "").split(/\s*\|\s*/)[0].trim();
}

/** A segment's whitespace-separated tokens. Quoting is not honoured, which
 *  can only cause a guard to stay silent, never to fire wrongly. */
/** @param {unknown} segment @returns {string[]} */
export function argsOf(segment) {
  const t = String(segment ?? "").trim();
  return t ? t.split(/\s+/) : [];
}

const EXCLUDES_GRAPHIFY_OUT = /(?:-e|--exclude)[=\s]+graphify-out\b/;

/** @param {string} argument @returns {boolean} */
function isFlag(argument) {
  return argument.startsWith("-");
}

/** True when the clean names a path to clean, which cannot reach
 *  graphify-out at the repository root. The value after -e or --exclude is
 *  that flag's, not a pathspec. */
/** @param {string[]} args @returns {boolean} */
function hasPathspec(args) {
  const after = args.slice(args.indexOf("clean") + 1);
  for (let i = 0; i < after.length; i++) {
    if (after[i] === "-e" || after[i] === "--exclude") {
      i++;
      continue;
    }
    if (!isFlag(after[i])) return true;
  }
  return false;
}

/** @param {string} segment @param {{ mergeInputsRan?: boolean }} _state @param {string|undefined} command @returns {string|null} */
function unexcludedClean(segment, _state, command) {
  const args = argsOf(segment);
  if (args[0] !== "git" || !args.includes("clean")) return null;
  const flags = args.filter((a) => /^-[^-]/.test(a)).join("");
  if (!(flags.includes("f") && flags.includes("d"))) return null;
  if (flags.includes("n") || args.includes("--dry-run")) return null;
  if (EXCLUDES_GRAPHIFY_OUT.test(segment)) return null;
  if (hasPathspec(args)) return null;
  // The context is read from the whole command, not this segment: the form an
  // agent writes is `cd repositories/<name> && git clean -fd`, where the cd is
  // a different segment. A clone and a store root that merely sits under a
  // directory named repositories share a path shape, so cwd cannot tell them
  // apart and is not consulted.
  if (!/(^|\s)repositories\//.test(maskQuoted(command))) return null;
  return (
    "This clean would delete the per-repo graphs. They live untracked at " +
    "repositories/<name>/graphify-out/, and a clean without the exclusion " +
    "forces a full re-extraction; it has destroyed 61 of 81 graphs here once. " +
    "Run: git clean -fd -e graphify-out"
  );
}

/** @param {string} segment @param {{ mergeInputsRan?: boolean }} _state @param {string|undefined} _command @returns {string|null} */
function indiscriminateStage(segment, _state, _command) {
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

/** @param {string[]} args @returns {boolean} */
function asksForHelp(args) {
  return args.includes("-h") || args.includes("--help");
}

/** @param {string} segment @param {{ mergeInputsRan?: boolean }} _state @param {string|undefined} _command @returns {string|null} */
function outsideExtraction(segment, _state, _command) {
  const args = argsOf(segment);
  if (asksForHelp(args)) return null;
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

// Real checkers only. A general-purpose interpreter in a pipeline is ordinary
// work, not a gate, so node, npm, npx and bare python are deliberately absent.
const CHECKERS = /^(?:pytest|ruff|pyright|tsc|eslint|mypy)\b|^python3?\s+-m\s+(?:unittest|pytest)\b/;
const PUBLISHES = /^git\s+(?:push|commit)\b/;

/** Reads the whole command rather than one segment: the hazard is the
 *  relationship between a piped checker and a later commit or push. Only an
 *  unbroken run of `&&` gates - `;` runs the next command whatever happened,
 *  and `||` runs it only on failure, so neither can be masked by the pipe. */
/** @param {string|undefined} command @returns {string|null} */
function pipedGate(command) {
  const links = chainLinks(command);
  for (let i = 0; i < links.length; i++) {
    if (!links[i].text.includes("|")) continue;
    if (!CHECKERS.test(pipelineHead(links[i].text))) continue;
    for (let j = i + 1; j < links.length && links[j].before === "&&"; j++) {
      if (PUBLISHES.test(links[j].text)) {
        return (
          "A pipeline's exit status is its last command's, not the checker's, " +
          "so this commits or pushes whatever the checker did. Redirect the " +
          "checker to a file and read it, or test ${PIPESTATUS[0]}."
        );
      }
    }
  }
  return null;
}

/** @param {string} segment @param {{ mergeInputsRan?: boolean }} state @param {string|undefined} _command @returns {string|null} */
function unreconciledMerge(segment, state, _command) {
  const args = argsOf(segment);
  if (asksForHelp(args)) return null;
  if (args[0] !== "graphify" || args[1] !== "merge-graphs") return null;
  if (state && state.mergeInputsRan) return null;
  return (
    "Run knowledgestore merge-inputs first and read its output. The merge is " +
    "driven by a shell glob, and a glob has picked up a previous run's outputs " +
    "here before - the merge reported a healthy count over the wrong inputs. " +
    "This guard does not inspect any graph; merge-inputs makes that judgement."
  );
}

const GUARDS = [unexcludedClean, indiscriminateStage, outsideExtraction, unreconciledMerge];

/** @typedef {{ allow: true } | { allow: false, deny: string }} Verdict */

/** @param {{ command?: string, state?: { mergeInputsRan?: boolean } }} [input] @returns {Verdict} */
export function decide({ command, state = {} } = {}) {
  const whole = pipedGate(command);
  if (whole) return { allow: false, deny: whole };
  for (const segment of chainSegments(command)) {
    for (const guard of GUARDS) {
      const deny = guard(segment, state, command);
      if (deny) return { allow: false, deny };
    }
  }
  return { allow: true };
}
