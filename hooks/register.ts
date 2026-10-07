import type { Register } from "claude-code";
// @ts-expect-error - a plain ES module beside this one, checked by its own harness
import { decide } from "./guards.mjs";

// A literal ref, because `claude plugin validate` holds every key to the contract.
const MERGE_INPUTS_RAN = { plugin: "knowledge-store", key: "mergeInputsRan" } as const;

export const register: Register = (on) => {
  on("tool.call", { tool: "Bash" }, async ($, e, next) => {
    try {
      const command = String(e.command ?? "");
      const held = await $.state.get(MERGE_INPUTS_RAN);
      const verdict = decide({
        command,
        state: { mergeInputsRan: held.value === true },
      });
      if (!verdict.allow) return { deny: verdict.deny };
      if (/\bknowledgestore\s+merge-inputs\b/.test(command)) {
        await $.state.set(MERGE_INPUTS_RAN, true);
      }
    } catch {
      // Fail open. These guards are an early warning, not a gate: the real
      // checks are in CI and the library, so a fault here must never stop a
      // command the operator is entitled to run.
    }
    return next(e);
  });
};
