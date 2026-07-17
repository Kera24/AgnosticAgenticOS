# Testing

- Test behaviour through public interfaces, not internals; a refactor that
  preserves behaviour should not break tests.
- Each test asserts one behaviour and is named for it; failure output must
  point at the cause without a debugger.
- Deterministic always: control time via injected clocks, seed randomness,
  fake the network and filesystem boundaries. No sleeps, no real
  providers, no ordering dependence.
- Cover the unhappy paths: invalid input, empty collections, boundary
  values, error propagation, concurrent/interrupted state.
- NEVER delete, skip, weaken, or broaden an assertion to make a suite
  pass; a failing test is information, not an obstacle.
- New code needs new tests in the same change; a bug fix starts with the
  failing test that reproduces it.
- Keep fixtures minimal and local; a test needing a 60-line setup is
  testing the wrong layer.
