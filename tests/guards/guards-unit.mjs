import assert from "node:assert/strict";
import { decide } from "../../hooks/guards.mjs";

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test("an unexcluded clean in a clone is refused", () => {
  const r = decide({ command: "git clean -fd", cwd: "/store/repositories/alpha" });
  assert.equal(r.allow, false);
  assert.match(r.deny, /graphify-out/);
});

test("the library's own clean is allowed", () => {
  const r = decide({ command: "git clean -fd -e graphify-out", cwd: "/store/repositories/alpha" });
  assert.equal(r.allow, true);
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

test("a dry-run clean is allowed", () => {
  assert.equal(decide({ command: "git clean -fdn", cwd: "/s/repositories/a" }).allow, true);
});

test("a pathspec-limited clean is allowed", () => {
  assert.equal(decide({ command: "git clean -fd src/", cwd: "/s/repositories/a" }).allow, true);
});

test("a store root merely under a repositories directory is not a clone", () => {
  assert.equal(decide({ command: "git clean -fd", cwd: "/home/u/repositories/mystore" }).allow, true);
});

test("the --exclude= form is honoured", () => {
  const r = decide({ command: "git clean -fd --exclude=graphify-out", cwd: "/s/repositories/a" });
  assert.equal(r.allow, true);
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

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log(`ok   ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message}`); }
}
console.log(`\n${cases.length - failed} of ${cases.length} passed`);
process.exit(failed ? 1 : 0);
