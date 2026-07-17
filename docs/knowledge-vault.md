# Knowledge Vault & Obsidian Setup

`.agentic/knowledge/` is a plain-Markdown vault maintained by
deterministic writers (`.agentic/core/knowledge.py`): project overview,
current state, architecture, requirements, milestones, decisions,
cycle retrospectives, and the final audit.

## Obsidian

Open `.agentic/knowledge/` as a vault (File → Open folder as vault), or:

```powershell
py .agentic/run knowledge open     # opens the folder; never automatic
```

`.obsidian/` workspace state is git-ignored and never indexed. The vault
uses standard Markdown + wiki links only — no plugin requirements.

## Editing rules

Each document has YAML frontmatter (id/type/project/status/created/
updated/source_revision/tags/content_hash) and ends with a marked user
section:

```markdown
<!-- user-notes:start -->
your notes here survive every regeneration
<!-- user-notes:end -->
```

- Notes inside the markers are preserved verbatim.
- Editing the **generated** area is detected: your file is left untouched
  and the fresh content lands beside it as `<name>.incoming.md`
  (reported by `knowledge status` and the dashboard).
- Unchanged content is never rewritten (stable `updated` stamps).

```powershell
py .agentic/run knowledge status
py .agentic/run knowledge rebuild
py .agentic/run knowledge validate
```

The broker retrieves relevant **sections** (never whole files) as
untrusted context.
