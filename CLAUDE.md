# Claude Code guide for knowledge-store-builder

## Graph handling

- **`deepdive extract` loads the full graph** (can be ~1.6 GB decompressed);
  `status` deliberately never does. Keep it that way.
- **`status` never returns non-zero.** Drift and coverage gaps are normal
  operating conditions; the stage reports, humans decide.
