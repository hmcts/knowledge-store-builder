import assert from "node:assert/strict";
import { decide } from "../../hooks/guards.mjs";

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test("the form an agent writes is refused", () => {
  const r = decide({ command: "cd repositories/alpha && git clean -fd" });
  assert.equal(r.allow, false);
  assert.match(r.deny, /graphify-out/);
});

test("the library's own clean is allowed", () => {
  const c = "cd repositories/alpha && git clean -fd -e graphify-out";
  assert.equal(decide({ command: c }).allow, true);
});

test("an empty command does not throw", () => {
  assert.equal(decide({ command: "   " }).allow, true);
});

test("git add -A is refused", () => {
  assert.equal(decide({ command: "git add -A" }).allow, false);
});

test("git add . is refused", () => {
  assert.equal(decide({ command: "git add ." }).allow, false);
});

test("git add of a named path is allowed", () => {
  assert.equal(decide({ command: "git add src/knowledgestore/cli.py" }).allow, true);
});

test("git add .gitignore is allowed", () => {
  assert.equal(decide({ command: "git add .gitignore" }).allow, true);
});

test("extraction given a repositories path is refused", () => {
  const r = decide({ command: "graphify update repositories/alpha" });
  assert.equal(r.allow, false);
  assert.match(r.deny, /from inside/);
});

test("extraction from inside the clone is allowed", () => {
  const r = decide({ command: "cd repositories/alpha && graphify update ." });
  assert.equal(r.allow, true);
});

test("the documented merge glob is not mistaken for an extraction", () => {
  const r = decide({
    command: "graphify merge-graphs repositories/*/graphify-out/graph.json --out graphify-out/graph.json",
    state: { mergeInputsRan: true },
  });
  assert.equal(r.allow, true);
});

test("a guard does not fire on its own name inside a quoted string", () => {
  assert.equal(decide({ command: "echo 'run git add -A never'" }).allow, true);
});

test("a checker piped and chained to a push is refused", () => {
  const r = decide({ command: "pytest | tail -1 && git push" });
  assert.equal(r.allow, false);
  assert.match(r.deny, /exit status/);
});

test("a checker piped with no push is allowed", () => {
  assert.equal(decide({ command: "pytest | tail -1" }).allow, true);
});

test("a checker chained to a push without a pipe is allowed", () => {
  assert.equal(decide({ command: "pytest && git push" }).allow, true);
});

test("|| is not mistaken for a pipe", () => {
  assert.equal(decide({ command: "pytest || git push" }).allow, true);
});

test("every segment is judged, not only the first", () => {
  const r = decide({ command: "echo one && git add -A" });
  assert.equal(r.allow, false);
});

test("a merge with no merge-inputs run is refused", () => {
  const r = decide({ command: "graphify merge-graphs a/graph.json --out out.json" });
  assert.equal(r.allow, false);
  assert.match(r.deny, /merge-inputs/);
});

test("a merge after merge-inputs is allowed", () => {
  const r = decide({
    command: "graphify merge-graphs a/graph.json --out out.json",
    state: { mergeInputsRan: true },
  });
  assert.equal(r.allow, true);
});

test("absent state behaves as not run rather than throwing", () => {
  const r = decide({ command: "graphify merge-graphs a/graph.json" });
  assert.equal(r.allow, false);
});

test("a quoted separator does not manufacture a segment", () => {
  assert.equal(decide({ command: 'git commit -m "do not; git add -A ever"' }).allow, true);
});

test("a piped checker followed by ; does not gate", () => {
  assert.equal(decide({ command: "pytest | tail -1; git commit -m x" }).allow, true);
});

test("the PIPESTATUS remedy the message recommends is allowed", () => {
  const c = "pytest | tee out.txt; test ${PIPESTATUS[0]} -eq 0 && git push";
  assert.equal(decide({ command: c }).allow, true);
});

test("a general-purpose interpreter in a pipeline is not a checker", () => {
  assert.equal(decide({ command: "python3 gen.py | jq . && git commit -m x" }).allow, true);
});

test("the --exclude= form is honoured", () => {
  const c = "cd repositories/alpha && git clean -fd --exclude=graphify-out";
  assert.equal(decide({ command: c }).allow, true);
});

test("a dry-run clean is allowed", () => {
  assert.equal(decide({ command: "cd repositories/alpha && git clean -fdn" }).allow, true);
});

test("a pathspec-limited clean is allowed", () => {
  assert.equal(decide({ command: "cd repositories/alpha && git clean -fd src/" }).allow, true);
});

test("a clean with no store context is allowed", () => {
  assert.equal(decide({ command: "git clean -fd" }).allow, true);
});

test("a repositories path inside quotes does not supply the context", () => {
  assert.equal(decide({ command: 'git clean -fd -m "repositories/x"' }).allow, true);
});

test("graphify extract from outside is refused too", () => {
  assert.equal(decide({ command: "graphify extract repositories/alpha" }).allow, false);
});

test("asking a guarded subcommand for help is allowed", () => {
  assert.equal(decide({ command: "graphify merge-graphs --help" }).allow, true);
});

test("git add -A keeps being refused even when a path is named", () => {
  assert.equal(decide({ command: "git add -A src/" }).allow, false);
});

test("a newline separates commands, so the clean is still caught", () => {
  assert.equal(decide({ command: "cd repositories/alpha\ngit clean -fd" }).allow, false);
});

test("a newline does not glue two commands into a false refusal", () => {
  assert.equal(decide({ command: "git add pyproject.toml\nruff format ." }).allow, true);
});

test("a newline breaks a gating run as a semicolon does", () => {
  assert.equal(decide({ command: "pytest | tail -1\ngit push" }).allow, true);
});

test("an operator keeps its chain across a line break", () => {
  assert.equal(decide({ command: "pytest | tail -1 &&\ngit push" }).allow, false);
});

test("a quoted exclusion is honoured", () => {
  const c = 'cd repositories/alpha && git clean -fd -e "graphify-out"';
  assert.equal(decide({ command: c }).allow, true);
});

test("an attached exclusion value is honoured", () => {
  const c = "cd repositories/alpha && git clean -fd -egraphify-out";
  assert.equal(decide({ command: c }).allow, true);
});

test("the PIPESTATUS remedy chained with && is allowed", () => {
  const c = "pytest | tee out.txt && test ${PIPESTATUS[0]} -eq 0 && git push";
  assert.equal(decide({ command: c }).allow, true);
});

test("reading the saved output before pushing is allowed", () => {
  const c = 'pytest | tee out.txt && grep -q "0 failed" out.txt && git push';
  assert.equal(decide({ command: c }).allow, true);
});

test("a piped checker pushed immediately is still refused", () => {
  assert.equal(decide({ command: "pytest | tail -1 && git push" }).allow, false);
});

test("merge-inputs earlier in the same command satisfies the precondition", () => {
  const c = "knowledgestore merge-inputs && graphify merge-graphs repositories/a/graphify-out/graph.json --out o.json";
  assert.equal(decide({ command: c }).allow, true);
});

test("the reverse order does not satisfy it", () => {
  const c = "graphify merge-graphs a/graph.json --out o.json && knowledgestore merge-inputs";
  assert.equal(decide({ command: c }).allow, false);
});

test("a flag's value is not read as a path", () => {
  const c = "graphify update . --exclude repositories/vendor";
  assert.equal(decide({ command: c }).allow, true);
});

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log(`ok   ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message}`); }
}
console.log(`\n${cases.length - failed} of ${cases.length} passed`);
process.exit(failed ? 1 : 0);
