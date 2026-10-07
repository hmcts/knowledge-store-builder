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

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log(`ok   ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message}`); }
}
console.log(`\n${cases.length - failed} of ${cases.length} passed`);
process.exit(failed ? 1 : 0);
