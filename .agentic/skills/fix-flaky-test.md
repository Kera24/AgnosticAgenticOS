# Skill: fix-flaky-test

Scope: stabilise a test that fails intermittently, keeping its assertions
meaningful.

- Trust name: `fix-flaky-test`.
- Typical done_when: the named test passes N consecutive runs
  (e.g. `python -m pytest tests/test_x.py -q --count 5` if pytest-repeat is
  available, otherwise a loop command).
- Never: delete the test, mark it skipped/xfail, widen assertions until they
  are meaningless, or add sleeps as the "fix" without a queue note.
- Root causes to look for: shared state, time/timezone, ordering, network.
