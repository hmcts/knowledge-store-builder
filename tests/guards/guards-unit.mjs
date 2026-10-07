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

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log(`ok   ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message}`); }
}
console.log(`\n${cases.length - failed} of ${cases.length} passed`);
process.exit(failed ? 1 : 0);
