import { expect, test } from "claude-code/testing";

// The bottom of the chain stands in for the Bash tool, so an allowed command
// is observed reaching it and nothing is actually run.
const MERGE = "graphify merge-graphs a.json b.json -o merged.json";

test("a refused command is denied through the hook", async ($, on) => {
  let reached = false;
  on("tool.call", { tool: "Bash" }, () => {
    reached = true;
    return { result: "" as never };
  });
  const out = await $.tool.call({ tool: "Bash", command: "git add -A" });
  // Breaks if the hook stops calling decide or stops returning { deny }.
  expect(out.deny).toMatch(/Stage explicit paths/);
  expect(reached).toBe(false);
});

test("a legitimate command passes through", async ($, on) => {
  let reached = false;
  on("tool.call", { tool: "Bash" }, () => {
    reached = true;
    return { result: "" as never };
  });
  const out = await $.tool.call({ tool: "Bash", command: "git add src/x.py" });
  // Breaks if the hook denies by default or throws on the happy path.
  expect(reached).toBe(true);
  expect(out.deny).toBeUndefined();
});

test("a merge is refused before merge-inputs and allowed after it", async ($, on) => {
  let reached = 0;
  on("tool.call", { tool: "Bash" }, () => {
    reached += 1;
    return { result: "" as never };
  });
  // Breaks if the state write is dropped or its key drifts from the key read:
  // the one behaviour that spans two calls.
  const before = await $.tool.call({ tool: "Bash", command: MERGE });
  expect(before.deny).toMatch(/Run knowledgestore merge-inputs first/);
  expect(reached).toBe(0);

  await $.tool.call({ tool: "Bash", command: "knowledgestore merge-inputs" });
  expect(reached).toBe(1);

  const after = await $.tool.call({ tool: "Bash", command: MERGE });
  expect(reached).toBe(2);
  expect(after.deny).toBeUndefined();
});
