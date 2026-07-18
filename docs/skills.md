# Skills — Security & Installation

Skills are third-party instruction packages (skills.sh-style layouts).
They are treated as **untrusted supply-chain artefacts** (ADR 0006).

## Installing (always explicit; never automatic)

```powershell
# 1. clone and pin the revision yourself (network stays outside the OS)
git clone https://example.com/some-skill C:\skills\some-skill
git -C C:\skills\some-skill checkout <commit>

# 2. install from the local checkout with the pinned revision
py .agentic/run skills add C:\skills\some-skill --revision <commit>

# 3. inspect, then review and enable
py .agentic/run skills inspect some-skill
py .agentic/run skills review some-skill
py .agentic/run skills enable some-skill
```

Unpinned sources are rejected. Installation checksums every file; a later
mismatch (`skills verify`) disables the skill and blocks loading. Skills
containing scripts install high-risk/read-only and cannot be enabled until
reviewed; scripts execute only through the command allowlist and only when
`skills.allow_scripts: true`.

## Runtime behaviour

- Selection is role-scoped and trigger-matched; disabled or unverified
  skills are never selected; at most `skills.max_injected` (default 2)
  per prompt, within the broker's skills allocation.
- Progressive loading: manifests → instructions for selected skills only
  → supporting files on explicit request.
- Skill text ranks below OS policy and renders as untrusted data; it can
  never modify policy or gain permissions.
- The model may *recommend* (`skills recommend <task-id>`), never install.

Five reviewed builtin skills ship with the OS: frontend-design,
uiux-design-systems, accessibility-review, testing, security-review.

## Marketplace lifecycle (MP Phase 5)

States: `discovered → quarantined → approved/enabled` (or `rejected`),
plus `update_available` and rollback. Sources are configured registries
(`skills.registries`: a directory of skill folders or an index file —
mirror skills.sh / the Anthropic marketplace / git checkouts locally; the
OS itself never fetches).

```powershell
agentic skills discover "pdf processing"   # metadata only, nothing downloads
agentic skills quarantine pdf-tools        # isolated copy, pinned + checksummed + scanned
agentic skills evaluate pdf-tools          # offline fixture evaluation + overlap report
agentic skills approve pdf-tools           # explicit human act; previous version preserved
agentic skills enable pdf-tools
agentic skills check-updates               # flags only — never updates silently
agentic skills compare pdf-tools           # file-level diff vs the update candidate
agentic skills rollback pdf-tools          # restore the preserved previous version
agentic skills project-skills              # regenerate claude/codex/qwen projections
```

The **Skill Curator** (used by agents) can search, analyse,
sandbox-evaluate, compare and recommend — it structurally has no
approve/install/enable/script surface. Checksum mismatches reject and
purge the candidate; prompt-injection patterns and scripts mark it
high-risk; the canonical skill stays owned by Agentic OS and provider
projections are regenerated, never edited.
