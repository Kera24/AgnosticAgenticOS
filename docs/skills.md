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
