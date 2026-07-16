# ADR 0006 — Skills supply-chain policy

Status: accepted · Date: 2026-07-17

## Context

Agent skills (e.g. from skills.sh) are third-party instructions and
sometimes scripts. Instructions are a prompt-injection vector; scripts are a
code-execution vector. Skills can nevertheless meaningfully improve
specialist workers (frontend design, accessibility, testing, security).

## Decision

Skills are treated as untrusted supply-chain artefacts:

- `auto_install: false` always; installation is an explicit admin action
  (`skills add <source> --revision <commit>`), pinned and checksummed.
- Manifests record licence, permissions, risk level, review status.
- Skill instructions rank below OS policy in the Context Broker and cannot
  modify security policy or request permissions.
- Skill scripts run only through the existing execpolicy allowlist and only
  when separately approved in configuration; new skills start read-only.
- Progressive loading: metadata → selected instructions → requested files;
  installed skills are never all injected into every prompt.

## Consequences

- Offline operation stays possible (skills are local once installed).
- Checksum mismatch or unpinned source hard-fails installation/loading.
- The model may *recommend* skills (`skills recommend`), never install them.
