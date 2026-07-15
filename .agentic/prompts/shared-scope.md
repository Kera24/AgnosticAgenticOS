# SHARED: SCOPE DISCIPLINE

- One task at a time. The smallest correct change wins.
- Stay inside `allowed_paths`; never touch `forbidden_paths` or protected
  paths.
- No unrelated refactoring, drive-by cleanups, speculative features, or new
  abstractions.
- Never delete, weaken, skip, or rewrite a test to make it pass.
- Never add a dependency; propose it as a queue item instead.
- If a required credential, endpoint, or undocumented decision is missing,
  stop and report a blocker instead of inventing one.
- "Done" is defined only by the work order's `done_when` conditions plus the
  deterministic checks. Nothing else counts.
