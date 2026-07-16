# ADR 0004 — Obsidian-compatible Markdown knowledge vault

Status: accepted · Date: 2026-07-17

## Context

Humans need a readable, durable view of architecture, decisions,
requirements, milestones, and audits. The SQLite memory store (ADR 0003) is
machine-oriented; YAML state files are operational, not narrative.

## Decision

Deterministic writers maintain a plain-Markdown vault at
`.agentic/knowledge/` with stable frontmatter (id/type/project/status/
created/updated/source_revision/tags) and consistent wiki-style links. The
directory opens directly as an Obsidian vault but depends on nothing
Obsidian-specific: standard Markdown only, `.obsidian/` git-ignored and
never indexed, no vault nesting. Files are rewritten only when content
changes; clearly marked user-editable sections are preserved and conflicts
are detected rather than overwritten.

## Consequences

- Knowledge is diffable in git and consumable without any tool.
- The broker retrieves relevant sections, never whole files by default.
- `knowledge open` may launch Obsidian/folder only on explicit user action —
  never during autonomous cycles.
