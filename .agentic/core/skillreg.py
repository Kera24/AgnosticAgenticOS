"""Secure, cross-model skill registry (ADR 0006).

Skills are third-party (or built-in) instruction packages compatible with
skills.sh-style layouts. Supply-chain rules enforced in code:

- automatic installation NEVER happens; `skills add` is an explicit admin
  action and requires a pinned revision;
- every skill's files are checksummed at install; a mismatch disables the
  skill and blocks loading;
- skill instructions are UNTRUSTED content, ranked below OS policy by the
  Context Broker; they can never modify policy or grant permissions;
- skill scripts are never executed by the registry; script execution goes
  through the existing execpolicy allowlist and only when
  `skills.allow_scripts` is true AND the skill's manifest was reviewed;
- skills with scripts install in read-only (disabled) state;
- progressive loading: manifests first, instructions only for selected
  skills, supporting files only on demand;
- selection is role-scoped and trigger-matched; disabled or unverified
  skills are never selected.

Registry: .agentic/skills/registry.yaml
Files:    .agentic/skills/installed/<id>/  (and shipped builtin/<id>/)
"""
import datetime as _dt
import hashlib
import os
import re
import shutil

import yaml

RISK_LEVELS = ("low", "medium", "high")
SCRIPT_EXTENSIONS = (".sh", ".ps1", ".py", ".js", ".bat", ".cmd", ".exe")
SUSPICIOUS_RE = re.compile(
    r"(?i)(curl\s+|wget\s+|invoke-webrequest|rm\s+-rf|del\s+/s|subprocess"
    r"|os\.system|eval\(|exec\(|base64\s+-d|iex\s)")

MANIFEST_FIELDS = (
    "id", "name", "description", "source", "pinned_revision", "checksum",
    "license", "compatible_agents", "compatible_backends", "triggers",
    "files", "scripts", "permissions", "reviewed", "reviewed_at",
    "enabled", "risk_level")


class SkillError(Exception):
    pass


def skills_config(cfg):
    raw = cfg.get("skills") or {}
    merged = {"enabled": True, "auto_install": False, "allow_scripts": False,
              "max_injected": 2}
    merged.update(raw)
    # auto_install is a policy line, not a preference: it cannot be enabled
    # by repository content, only by the machine administrator's config —
    # and even then installation still requires an explicit CLI action.
    return merged


class SkillRegistry:
    def __init__(self, cfg, agentic_dir, clock=None):
        self.cfg = cfg
        self.agentic_dir = str(agentic_dir)
        self.root = os.path.join(self.agentic_dir, "skills")
        self.registry_path = os.path.join(self.root, "registry.yaml")
        self.clock = clock or _dt.datetime.now

    # -- registry file --------------------------------------------------------
    def _load(self):
        if not os.path.exists(self.registry_path):
            return {"skills": []}
        with open(self.registry_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        data.setdefault("skills", [])
        return data

    def _save(self, data):
        os.makedirs(self.root, exist_ok=True)
        tmp = self.registry_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
        os.replace(tmp, self.registry_path)

    def list(self):
        self.ensure_builtins()
        return self._load()["skills"]

    def get(self, skill_id):
        for skill in self.list():
            if skill["id"] == skill_id:
                return skill
        return None

    # -- checksums -----------------------------------------------------------
    def skill_dir(self, manifest):
        base = "builtin" if manifest.get("source") == "builtin" \
            else "installed"
        return os.path.join(self.root, base, manifest["id"])

    @staticmethod
    def compute_checksum(directory):
        digest = hashlib.sha256()
        entries = []
        for base, _dirs, files in os.walk(directory):
            for name in sorted(files):
                full = os.path.join(base, name)
                rel = os.path.relpath(full, directory).replace("\\", "/")
                with open(full, "rb") as fh:
                    entries.append((rel,
                                    hashlib.sha256(fh.read()).hexdigest()))
        for rel, file_hash in sorted(entries):
            digest.update(("%s\0%s\n" % (rel, file_hash)).encode())
        return digest.hexdigest()

    def verify(self, skill_id):
        """Recompute the checksum; on mismatch the skill is disabled and
        flagged. Returns the verification dict."""
        manifest = self.get(skill_id)
        if manifest is None:
            raise SkillError("unknown skill %r" % skill_id)
        directory = self.skill_dir(manifest)
        if not os.path.isdir(directory):
            return self._flag(skill_id, "files missing")
        actual = self.compute_checksum(directory)
        if actual != manifest.get("checksum"):
            return self._flag(skill_id, "checksum mismatch")
        return {"id": skill_id, "ok": True, "checksum": actual}

    def _flag(self, skill_id, reason):
        data = self._load()
        for skill in data["skills"]:
            if skill["id"] == skill_id:
                skill["enabled"] = False
                skill["integrity_failure"] = reason
        self._save(data)
        return {"id": skill_id, "ok": False, "reason": reason}

    # -- install / manage -----------------------------------------------------------
    def add(self, source, revision=None, skill_id=None):
        """Explicit admin installation from a LOCAL directory (a cloned,
        pinned checkout). Remote fetching is deliberately out of scope: the
        admin clones/checks out the pinned revision first, keeping network
        access out of the OS. Unpinned sources are rejected."""
        if not revision:
            raise SkillError("a pinned revision is required "
                             "(skills add <source> --revision <commit>)")
        if not os.path.isdir(source):
            raise SkillError("source %r is not a local directory; clone the "
                             "pinned revision first" % source)
        manifest_path = _find_manifest(source)
        meta = {}
        if manifest_path:
            with open(manifest_path, encoding="utf-8") as fh:
                meta = yaml.safe_load(fh) or {}
        skill_id = skill_id or meta.get("id") or \
            os.path.basename(os.path.normpath(source)).lower()
        if self.get(skill_id):
            raise SkillError("skill %r already installed" % skill_id)

        files, scripts, findings = _scan(source)
        risk = "high" if scripts or findings else \
            meta.get("risk_level", "low")
        dest = os.path.join(self.root, "installed", skill_id)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copytree(source, dest)
        checksum = self.compute_checksum(dest)
        manifest = {
            "id": skill_id,
            "name": meta.get("name", skill_id),
            "description": str(meta.get("description", ""))[:300],
            "source": str(source),
            "pinned_revision": str(revision),
            "checksum": checksum,
            "license": meta.get("license") or _read_license(source),
            "compatible_agents": meta.get("compatible_agents") or ["*"],
            "compatible_backends": meta.get("compatible_backends") or ["*"],
            "triggers": meta.get("triggers") or [],
            "files": files,
            "scripts": scripts,
            "permissions": ["read"],       # new skills start read-only
            "reviewed": False,
            "reviewed_at": None,
            "enabled": False,              # explicit enable required
            "risk_level": risk if risk in RISK_LEVELS else "high",
            "scan_findings": findings,
        }
        data = self._load()
        data["skills"].append(manifest)
        self._save(data)
        return manifest

    def enable(self, skill_id):
        manifest = self.get(skill_id)
        if manifest is None:
            raise SkillError("unknown skill %r" % skill_id)
        check = self.verify(skill_id)
        if not check["ok"]:
            raise SkillError("cannot enable %s: %s"
                             % (skill_id, check["reason"]))
        if manifest.get("scripts") and manifest.get("risk_level") == "high" \
                and not manifest.get("reviewed"):
            raise SkillError("skill %s contains scripts and is unreviewed; "
                             "review it first (skills inspect)" % skill_id)
        return self._set(skill_id, enabled=True)

    def disable(self, skill_id):
        return self._set(skill_id, enabled=False)

    def mark_reviewed(self, skill_id):
        return self._set(skill_id, reviewed=True,
                         reviewed_at=self.clock().isoformat(
                             timespec="seconds"))

    def remove(self, skill_id):
        manifest = self.get(skill_id)
        if manifest is None:
            raise SkillError("unknown skill %r" % skill_id)
        if manifest.get("source") == "builtin":
            raise SkillError("builtin skills are disabled, not removed")
        data = self._load()
        data["skills"] = [s for s in data["skills"]
                          if s["id"] != skill_id]
        self._save(data)
        shutil.rmtree(self.skill_dir(manifest), ignore_errors=True)
        return {"removed": skill_id}

    def _set(self, skill_id, **fields):
        data = self._load()
        for skill in data["skills"]:
            if skill["id"] == skill_id:
                skill.update(fields)
                self._save(data)
                return skill
        raise SkillError("unknown skill %r" % skill_id)

    # -- selection & progressive loading ------------------------------------------
    def select(self, role, query, limit=None):
        """Metadata-only selection: enabled, integrity-verified,
        role-compatible skills whose triggers match the query. Never all
        skills, never full instructions at this stage."""
        scfg = skills_config(self.cfg)
        if not scfg["enabled"]:
            return []
        limit = limit or int(scfg["max_injected"])
        low = (query or "").lower()
        scored = []
        for manifest in self.list():
            if not manifest.get("enabled"):
                continue
            agents = manifest.get("compatible_agents") or []
            if "*" not in agents and role not in agents:
                continue
            triggers = [str(t).lower()
                        for t in manifest.get("triggers") or []]
            score = sum(1 for t in triggers if t and t in low)
            if score <= 0:
                continue
            if not self.verify(manifest["id"])["ok"]:
                continue
            scored.append((score, manifest))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [m for _s, m in scored[:limit]]

    def instructions(self, skill_id):
        """Load the full instruction text for one selected skill."""
        manifest = self.get(skill_id)
        if manifest is None or not manifest.get("enabled"):
            raise SkillError("skill %r is not available" % skill_id)
        if not self.verify(skill_id)["ok"]:
            raise SkillError("skill %r failed integrity check" % skill_id)
        directory = self.skill_dir(manifest)
        for name in ("SKILL.md", "skill.md", "README.md"):
            path = os.path.join(directory, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    return fh.read()
        raise SkillError("skill %r has no instruction file" % skill_id)

    def load_file(self, skill_id, rel):
        """Supporting file, on explicit request only. Path-confined."""
        manifest = self.get(skill_id)
        if manifest is None:
            raise SkillError("unknown skill %r" % skill_id)
        directory = os.path.realpath(self.skill_dir(manifest))
        full = os.path.realpath(os.path.join(directory, rel))
        if not full.startswith(directory + os.sep):
            raise SkillError("path escapes the skill directory")
        with open(full, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    # -- builtin, reviewed starter skills ----------------------------------------
    def ensure_builtins(self):
        """Register the shipped, reviewed starter skills (idempotent).
        Their integrity is pinned by checksum like any other skill."""
        data = self._load()
        known = {s["id"] for s in data["skills"]}
        builtin_root = os.path.join(self.root, "builtin")
        if not os.path.isdir(builtin_root):
            return
        changed = False
        for skill_id in sorted(os.listdir(builtin_root)):
            directory = os.path.join(builtin_root, skill_id)
            if not os.path.isdir(directory) or skill_id in known:
                continue
            meta_path = _find_manifest(directory)
            meta = {}
            if meta_path:
                with open(meta_path, encoding="utf-8") as fh:
                    meta = yaml.safe_load(fh) or {}
            data["skills"].append({
                "id": skill_id, "name": meta.get("name", skill_id),
                "description": str(meta.get("description", ""))[:300],
                "source": "builtin", "pinned_revision": "builtin",
                "checksum": self.compute_checksum(directory),
                "license": meta.get("license", "repository"),
                "compatible_agents": meta.get("compatible_agents") or ["*"],
                "compatible_backends": ["*"],
                "triggers": meta.get("triggers") or [],
                "files": _scan(directory)[0], "scripts": [],
                "permissions": ["read"], "reviewed": True,
                "reviewed_at": self.clock().isoformat(timespec="seconds"),
                "enabled": True, "risk_level": "low"})
            changed = True
        if changed:
            self._save(data)


def skill_items(cfg, agentic_dir, role, query):
    """Selected skill instructions as UNTRUSTED ContextItems for the broker
    (the skills budget/allocation applies there)."""
    from .context.items import ContextItem
    try:
        registry = SkillRegistry(cfg, agentic_dir)
        selected = registry.select(role, query)
    except Exception:   # noqa: BLE001 — skills never break an invocation
        return []
    items = []
    for manifest in selected:
        try:
            text = registry.instructions(manifest["id"])
        except SkillError:
            continue
        items.append(ContextItem(
            "skill",
            "[skill %s @ %s]\n%s" % (manifest["id"],
                                     manifest["pinned_revision"], text),
            source_type="skill", source_path=manifest["id"],
            relevance_score=0.6, trust_level="untrusted",
            metadata={"risk_level": manifest.get("risk_level")}))
    return items


def _find_manifest(directory):
    for name in ("skill.yaml", "skill.yml", "manifest.yaml",
                 "manifest.yml"):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return path
    return None


def _read_license(directory):
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.readline().strip()[:100] or "present"
    return "unknown"


def _scan(directory):
    """Inventory files, flag scripts and suspicious content."""
    files, scripts, findings = [], [], []
    for base, _dirs, names in os.walk(directory):
        for name in sorted(names):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, directory).replace("\\", "/")
            files.append(rel)
            if name.lower().endswith(SCRIPT_EXTENSIONS):
                scripts.append(rel)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    content = fh.read(200_000)
            except OSError:
                continue
            for match in SUSPICIOUS_RE.finditer(content):
                findings.append("%s: suspicious token %r"
                                % (rel, match.group(0).strip()))
    return files, scripts, findings[:20]
