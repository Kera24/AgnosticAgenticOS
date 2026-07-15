# Skill: fix-lint-debt

Scope: resolve existing linter warnings/errors without changing behaviour.

- Trust name: `fix-lint-debt` (must be reused verbatim by the conductor).
- Typical done_when: lint command exits 0 for the touched files; test suite
  unchanged and passing.
- Allowed paths: only the files named in the findings.
- Never: disable lint rules, add ignore pragmas to silence real issues,
  reformat unrelated files.
