import assert from "node:assert/strict";
import { decide, maskQuoted } from "../../hooks/guards.mjs";

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

// Every row names the break it catches: the command, the verdict decide must
// reach, and where it matters the pattern its refusal has to carry.
/** @type {[string, string, boolean, RegExp?, object?][]} */
const VERDICTS = [
  ["the form an agent writes is refused", "cd repositories/alpha && git clean -fd", false, /graphify-out/],
  ["the library's own clean is allowed", "cd repositories/alpha && git clean -fd -e graphify-out", true],
  ["an empty command does not throw", "   ", true],
  ["git add -A is refused", "git add -A", false],
  ["git add . is refused", "git add .", false],
  ["git add of a named path is allowed", "git add src/knowledgestore/cli.py", true],
  ["git add .gitignore is allowed", "git add .gitignore", true],
  ["extraction given a repositories path is refused", "graphify update repositories/alpha", false, /from inside/],
  ["extraction from inside the clone is allowed", "cd repositories/alpha && graphify update .", true],
  [
    "the documented merge glob is not mistaken for an extraction",
    "graphify merge-graphs repositories/*/graphify-out/graph.json --out graphify-out/graph.json",
    true,
    undefined,
    { mergeInputsRan: true },
  ],
  ["a guard does not fire on its own name inside a quoted string", "echo 'run git add -A never'", true],
  ["a checker piped and chained to a push is refused", "pytest | tail -1 && git push", false, /exit status/],
  ["a checker piped with no push is allowed", "pytest | tail -1", true],
  ["a checker chained to a push without a pipe is allowed", "pytest && git push", true],
  ["|| is not mistaken for a pipe", "pytest || git push", true],
  ["every segment is judged, not only the first", "echo one && git add -A", false],
  ["a merge with no merge-inputs run is refused", "graphify merge-graphs a/graph.json --out out.json", false, /merge-inputs/],
  [
    "a merge after merge-inputs is allowed",
    "graphify merge-graphs a/graph.json --out out.json",
    true,
    undefined,
    { mergeInputsRan: true },
  ],
  ["absent state behaves as not run rather than throwing", "graphify merge-graphs a/graph.json", false],
  ["a quoted separator does not manufacture a segment", 'git commit -m "do not; git add -A ever"', true],
  ["a piped checker followed by ; does not gate", "pytest | tail -1; git commit -m x", true],
  [
    "the PIPESTATUS remedy the message recommends is allowed",
    "pytest | tee out.txt; test ${PIPESTATUS[0]} -eq 0 && git push",
    true,
  ],
  ["a general-purpose interpreter in a pipeline is not a checker", "python3 gen.py | jq . && git commit -m x", true],
  ["the --exclude= form is honoured", "cd repositories/alpha && git clean -fd --exclude=graphify-out", true],
  ["a dry-run clean is allowed", "cd repositories/alpha && git clean -fdn", true],
  ["a pathspec-limited clean is allowed", "cd repositories/alpha && git clean -fd src/", true],
  ["a clean with no store context is allowed", "git clean -fd", true],
  ["a repositories path inside quotes does not supply the context", 'git clean -fd -m "repositories/x"', true],
  ["graphify extract from outside is refused too", "graphify extract repositories/alpha", false],
  ["asking a guarded subcommand for help is allowed", "graphify merge-graphs --help", true],
  ["git add -A keeps being refused even when a path is named", "git add -A src/", false],
  ["a newline separates commands, so the clean is still caught", "cd repositories/alpha\ngit clean -fd", false],
  ["a newline does not glue two commands into a false refusal", "git add pyproject.toml\nruff format .", true],
  ["a newline breaks a gating run as a semicolon does", "pytest | tail -1\ngit push", true],
  ["an operator keeps its chain across a line break", "pytest | tail -1 &&\ngit push", false],
  ["a quoted exclusion is honoured", 'cd repositories/alpha && git clean -fd -e "graphify-out"', true],
  ["an attached exclusion value is honoured", "cd repositories/alpha && git clean -fd -egraphify-out", true],
  [
    "the PIPESTATUS remedy chained with && is allowed",
    "pytest | tee out.txt && test ${PIPESTATUS[0]} -eq 0 && git push",
    true,
  ],
  [
    "reading the saved output before pushing is allowed",
    'pytest | tee out.txt && grep -q "0 failed" out.txt && git push',
    true,
  ],
  ["a piped checker pushed immediately is still refused", "pytest | tail -1 && git push", false],
  [
    "merge-inputs earlier in the same command satisfies the precondition",
    "knowledgestore merge-inputs && graphify merge-graphs repositories/a/graphify-out/graph.json --out o.json",
    true,
  ],
  [
    "the reverse order does not satisfy it",
    "graphify merge-graphs a/graph.json --out o.json && knowledgestore merge-inputs",
    false,
  ],
  ["a flag's value is not read as a path", "graphify update . --exclude repositories/vendor", true],
  [
    "an emoji inside a quote does not shift the later segments",
    'echo "\u{1F600}\u{1F600}\u{1F600} x" && cd repositories/a && git clean -fd -e "graphify-out" && git add pyproject.toml',
    true,
  ],
];

for (const [name, command, allow, deny, state] of VERDICTS) {
  test(name, () => {
    // Absent state is itself a case ("absent state behaves as not run"), so it
    // is only passed when the row supplies one.
    const r = decide(state ? { command, state } : { command });
    assert.equal(r.allow, allow);
    if (deny) assert.match(r.deny, deny);
  });
}

// Asserts something other than a verdict, so it stays out of the table.
test("masking preserves length so raw slices stay aligned", () => {
  const c = 'echo "\u{1F600} a; b" && cd x';
  assert.equal(maskQuoted(c).length, c.length);
});

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log(`ok   ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message}`); }
}
console.log(`\n${cases.length - failed} of ${cases.length} passed`);
process.exit(failed ? 1 : 0);
