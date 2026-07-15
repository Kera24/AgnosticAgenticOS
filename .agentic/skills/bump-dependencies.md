# Skill: bump-dependencies

Scope: propose dependency updates. ALWAYS queued — dependency changes are a
MUST QUEUE contract area, so this skill can never act autonomously.

- Trust name: `bump-dependencies` (listed in trust.sensitive_skills by
  convention; the policy gate also queues any work order whose allowed_paths
  touch a dependency manifest).
- Output of a run is a queue item containing: the manifest diff, changelog
  links as evidence, and the deterministic check results against the bump.
