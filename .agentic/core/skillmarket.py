"""Managed skills marketplace: the lifecycle AROUND the existing pinned
skill registry (core/skillreg.py), which stays the single installed-skill
authority.

Lifecycle:  discovered -> quarantined -> (evaluated) -> approved/enabled
                                 \\-> rejected
            installed -> update_available -> (compare, approve) -> updated
                                 \\-> rollback to previous_revision

Storage (machine-local runtime home, never in any repo):
    <home>/skills/catalog.json        candidate metadata (discovery)
    <home>/skills/quarantine/<id>/    isolated downloaded candidates
    <home>/skills/rollback/<id>/      previous approved version
    <home>/skills/projections/<agent>/<id>/   provider projections

Sources are configured registries (`skills.registries`) that the OS reads
LOCALLY — a directory of skill folders, or an index file. Remote catalogs
(skills.sh, the Anthropic marketplace, git repositories) are mirrored to a
local path by the administrator; the OS itself performs no network fetch,
consistent with ADR 0006.

The Skill Curator may search, compare, analyse, sandbox-evaluate and
recommend. It structurally CANNOT approve, install, execute scripts, or
expand permissions — those methods simply do not exist on it.
"""
import datetime as _dt
import hashlib
import json
import os
import re
import shutil

import yaml

from .skillreg import SkillError, SkillRegistry, _scan

STATES = ("discovered", "quarantined", "approved", "enabled", "disabled",
          "rejected", "update_available", "deprecated")

INJECTION_RE = re.compile(
    r"(?i)(ignore (all )?(previous|prior) instructions|disregard.{0,20}"
    r"(policy|instructions)|you are now|system prompt|exfiltrat|"
    r"reveal.{0,20}(secret|key|token)|push to (origin|main))")

MAX_INSTRUCTION_BYTES = 200_000


def _now():
    return _dt.datetime.now().isoformat(timespec="seconds")


def market_paths(home):
    base = os.path.join(home, "skills")
    return {"base": base,
            "catalog": os.path.join(base, "catalog.json"),
            "quarantine": os.path.join(base, "quarantine"),
            "rollback": os.path.join(base, "rollback"),
            "projections": os.path.join(base, "projections")}


class SkillMarket:
    def __init__(self, cfg, agentic_dir, home, clock=None):
        self.cfg = cfg
        self.registry = SkillRegistry(cfg, agentic_dir, clock=clock)
        self.paths = market_paths(home)
        self.clock = clock or _now

    # -- catalog --------------------------------------------------------------
    def _load_catalog(self):
        path = self.paths["catalog"]
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return {}

    def _save_catalog(self, catalog):
        os.makedirs(self.paths["base"], exist_ok=True)
        tmp = self.paths["catalog"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(catalog, fh, indent=2, default=str)
        os.replace(tmp, self.paths["catalog"])

    def candidate(self, skill_id):
        record = self._load_catalog().get(skill_id)
        if record is None:
            raise SkillError("unknown candidate %r (discover first)"
                             % skill_id)
        return record

    def _set_candidate(self, skill_id, **fields):
        catalog = self._load_catalog()
        record = catalog.get(skill_id)
        if record is None:
            raise SkillError("unknown candidate %r" % skill_id)
        record.update(fields, updated_at=_now())
        self._save_catalog(catalog)
        return record

    # -- discovery (metadata only; no files leave their source) ----------------
    def registries(self):
        return (self.cfg.get("skills") or {}).get("registries") or []

    def discover(self, query):
        """Search installed skills first, then configured registries.
        Candidates are stored as METADATA ONLY — nothing is downloaded."""
        query_low = (query or "").lower()
        installed = [s for s in self.registry.list()
                     if _matches(s, query_low)]
        catalog = self._load_catalog()
        found = []
        for source in self.registries():
            for meta in _read_source(source):
                if not _matches(meta, query_low):
                    continue
                skill_id = meta["id"]
                if self.registry.get(skill_id):
                    continue                     # already installed
                existing = catalog.get(skill_id) or {}
                record = {
                    "id": skill_id, "name": meta.get("name", skill_id),
                    "description": str(meta.get("description", ""))[:300],
                    "source_type": source.get("type"),
                    "source_url": source.get("url") or source.get("path"),
                    "repository": meta.get("repository"),
                    "pinned_revision": None,
                    "current_revision": meta.get("revision"),
                    "previous_revision": None,
                    "version": meta.get("version"),
                    "checksum": meta.get("checksum"),
                    "licence": meta.get("license") or meta.get("licence"),
                    "author": meta.get("author"),
                    "triggers": meta.get("triggers") or [],
                    "compatible_agents": meta.get("compatible_agents")
                    or ["*"],
                    "compatible_backends": ["*"],
                    "permissions": [], "scripts": [], "dependencies":
                        meta.get("dependencies") or [],
                    "risk_level": "unknown",
                    "state": existing.get("state", "discovered"),
                    "discovered_at": existing.get("discovered_at", _now()),
                    "reviewed_at": existing.get("reviewed_at"),
                    "enabled": False,
                    "evaluation_result": existing.get("evaluation_result"),
                    "local_path": meta.get("local_path"),
                }
                catalog[skill_id] = record
                found.append(record)
        self._save_catalog(catalog)
        return {"installed_matches": installed, "candidates": found}

    # -- quarantine ---------------------------------------------------------------
    def quarantine(self, skill_id):
        """Copy the candidate into isolated quarantine storage, pin its
        revision, compute/verify the checksum, and scan it. Nothing is
        installed."""
        record = self.candidate(skill_id)
        source_path = record.get("local_path")
        if not source_path or not os.path.isdir(source_path):
            raise SkillError(
                "candidate %s has no local mirror to quarantine from; "
                "mirror the pinned revision locally first" % skill_id)
        if not record.get("current_revision"):
            raise SkillError("candidate %s has no revision to pin; refuse "
                             "unpinned material" % skill_id)
        dest = os.path.join(self.paths["quarantine"], skill_id)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copytree(source_path, dest)
        checksum = SkillRegistry.compute_checksum(dest)
        if record.get("checksum") and record["checksum"] != checksum:
            shutil.rmtree(dest, ignore_errors=True)
            self._set_candidate(skill_id, state="rejected",
                                evaluation_result={
                                    "verdict": "rejected",
                                    "reason": "checksum mismatch against "
                                              "registry metadata"})
            raise SkillError("checksum mismatch for %s; candidate rejected "
                             "and purged" % skill_id)
        files, scripts, findings = _scan(dest)
        injection = _injection_findings(dest)
        licence = record.get("licence") or _read_licence(dest)
        return self._set_candidate(
            skill_id, state="quarantined",
            pinned_revision=record["current_revision"],
            checksum=checksum, licence=licence,
            scripts=scripts,
            risk_level="high" if (scripts or findings or injection)
            else "low",
            evaluation_result={
                "scanned_at": _now(), "files": len(files),
                "script_findings": findings,
                "injection_findings": injection})

    # -- offline evaluation --------------------------------------------------------
    def evaluate(self, skill_id):
        """Deterministic fixture-based evaluation of a quarantined
        candidate; produces the comparison report against installed
        skills with overlapping triggers."""
        record = self.candidate(skill_id)
        if record["state"] not in ("quarantined", "update_available"):
            raise SkillError("evaluate requires a quarantined candidate "
                             "(state is %s)" % record["state"])
        directory = os.path.join(self.paths["quarantine"], skill_id)
        checks = {}
        instruction = _instruction_file(directory)
        checks["has_instructions"] = instruction is not None
        if instruction:
            size = os.path.getsize(instruction)
            checks["instruction_size_ok"] = 0 < size <= \
                MAX_INSTRUCTION_BYTES
        checks["has_triggers"] = bool(record.get("triggers"))
        checks["licence_known"] = bool(record.get("licence")) and \
            record["licence"] != "unknown"
        checks["no_injection_patterns"] = not (
            (record.get("evaluation_result") or {})
            .get("injection_findings"))
        checks["no_unreviewed_scripts"] = not record.get("scripts")
        overlaps = []
        for installed in self.registry.list():
            shared = set(map(str.lower, installed.get("triggers") or [])) \
                & set(map(str.lower, record.get("triggers") or []))
            if shared:
                overlaps.append({"installed": installed["id"],
                                 "shared_triggers": sorted(shared)})
        passed = sum(1 for v in checks.values() if v)
        result = dict(record.get("evaluation_result") or {},
                      checks=checks, score="%d/%d" % (passed, len(checks)),
                      overlapping_installed=overlaps,
                      evaluated_at=_now(),
                      verdict="recommend" if passed == len(checks)
                      else "review_findings")
        return self._set_candidate(skill_id, evaluation_result=result)

    # -- approval / rejection (EXPLICIT admin actions) ------------------------------
    def approve(self, skill_id):
        """Install a quarantined candidate into the active registry at its
        pinned revision. Preserves any previously installed version for
        rollback. Never called by the curator or any agent."""
        record = self.candidate(skill_id)
        if record["state"] not in ("quarantined", "update_available"):
            raise SkillError("approve requires a quarantined candidate "
                             "(state is %s)" % record["state"])
        if not record.get("pinned_revision"):
            raise SkillError("candidate %s is not pinned" % skill_id)
        directory = os.path.join(self.paths["quarantine"], skill_id)
        if not os.path.isdir(directory):
            raise SkillError("quarantine files for %s are missing"
                             % skill_id)
        previous = self.registry.get(skill_id)
        previous_revision = None
        if previous:
            previous_revision = previous.get("pinned_revision")
            backup = os.path.join(self.paths["rollback"], skill_id)
            if os.path.isdir(backup):
                shutil.rmtree(backup)
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copytree(self.registry.skill_dir(previous), backup)
            with open(os.path.join(backup, ".agentic-revision"), "w",
                      encoding="utf-8") as fh:
                fh.write(previous_revision or "")
            self.registry.remove(skill_id)
        self.registry.add(directory, revision=record["pinned_revision"],
                          skill_id=skill_id)
        self.registry.mark_reviewed(skill_id)
        self._set_candidate(skill_id, state="approved",
                            previous_revision=previous_revision,
                            reviewed_at=_now())
        return self.registry.get(skill_id)

    def reject(self, skill_id):
        """Reject and PURGE the quarantined files."""
        self._set_candidate(skill_id, state="rejected")
        shutil.rmtree(os.path.join(self.paths["quarantine"], skill_id),
                      ignore_errors=True)
        return {"rejected": skill_id, "quarantine_purged": True}

    # -- updates ---------------------------------------------------------------------
    def check_updates(self):
        """Mark installed skills whose registry source advertises a newer
        revision. NOTHING updates automatically."""
        flagged = []
        sources = {meta["id"]: meta for source in self.registries()
                   for meta in _read_source(source)}
        catalog = self._load_catalog()
        for installed in self.registry.list():
            meta = sources.get(installed["id"])
            if not meta or not meta.get("revision"):
                continue
            if meta["revision"] != installed.get("pinned_revision"):
                entry = catalog.get(installed["id"]) or {
                    "id": installed["id"], "discovered_at": _now()}
                entry.update(
                    state="update_available",
                    current_revision=meta["revision"],
                    pinned_revision=None,
                    previous_revision=installed.get("pinned_revision"),
                    checksum=meta.get("checksum"),
                    local_path=meta.get("local_path"),
                    triggers=installed.get("triggers"),
                    name=installed.get("name"),
                    description=installed.get("description"))
                catalog[installed["id"]] = entry
                flagged.append(installed["id"])
        self._save_catalog(catalog)
        return {"update_available": flagged}

    def compare(self, skill_id):
        """File-level comparison between the installed version and the
        quarantined update candidate."""
        installed = self.registry.get(skill_id)
        if installed is None:
            raise SkillError("skill %r is not installed" % skill_id)
        directory = os.path.join(self.paths["quarantine"], skill_id)
        if not os.path.isdir(directory):
            raise SkillError("no quarantined candidate for %s (run "
                             "quarantine after check-updates)" % skill_id)
        old_dir = self.registry.skill_dir(installed)
        old_files = _file_hashes(old_dir)
        new_files = _file_hashes(directory)
        return {
            "skill": skill_id,
            "installed_revision": installed.get("pinned_revision"),
            "candidate_revision":
                self.candidate(skill_id).get("current_revision"),
            "added": sorted(set(new_files) - set(old_files)),
            "removed": sorted(set(old_files) - set(new_files)),
            "changed": sorted(k for k in set(old_files) & set(new_files)
                              if old_files[k] != new_files[k]),
        }

    def rollback(self, skill_id):
        """Restore the preserved previous version of an installed skill."""
        backup = os.path.join(self.paths["rollback"], skill_id)
        if not os.path.isdir(backup):
            raise SkillError("no previous version preserved for %s"
                             % skill_id)
        rev_file = os.path.join(backup, ".agentic-revision")
        revision = "unknown"
        if os.path.exists(rev_file):
            with open(rev_file, encoding="utf-8") as fh:
                revision = fh.read().strip() or "unknown"
            os.remove(rev_file)
        if self.registry.get(skill_id):
            self.registry.remove(skill_id)
        manifest = self.registry.add(backup, revision=revision,
                                     skill_id=skill_id)
        self.registry.mark_reviewed(skill_id)
        self.registry.enable(skill_id)
        with open(rev_file, "w", encoding="utf-8") as fh:
            fh.write(revision)
        catalog = self._load_catalog()
        if skill_id in catalog:
            catalog[skill_id]["state"] = "approved"
            self._save_catalog(catalog)
        return manifest

    # -- provider projections ----------------------------------------------------------
    def project_skills(self):
        """Write provider-specific projections of the canonical enabled
        skills. The canonical files stay owned by Agentic OS; projections
        are regenerated, never edited."""
        written = {}
        for agent in ("claude", "codex", "qwen", "generic"):
            agent_dir = os.path.join(self.paths["projections"], agent)
            if os.path.isdir(agent_dir):
                shutil.rmtree(agent_dir)
            for manifest in self.registry.list():
                if not manifest.get("enabled"):
                    continue
                try:
                    text = self.registry.instructions(manifest["id"])
                except SkillError:
                    continue
                dest = os.path.join(agent_dir, manifest["id"])
                os.makedirs(dest, exist_ok=True)
                with open(os.path.join(dest, "SKILL.md"), "w",
                          encoding="utf-8", newline="\n") as fh:
                    fh.write("---\nname: %s\ndescription: %s\n"
                             "canonical: agentic-os\nrevision: %s\n---\n\n"
                             % (manifest["id"],
                                manifest.get("description", "")
                                .replace("\n", " ")[:200],
                                manifest.get("pinned_revision")))
                    fh.write(text)
                written.setdefault(agent, []).append(manifest["id"])
        return written


class SkillCurator:
    """Restricted curator: search, compare, analyse, sandbox-evaluate,
    recommend. It has NO approve/install/enable/script-execution surface —
    a structural guarantee, not a prompt-level one."""

    def __init__(self, market):
        self._market = market

    def search(self, query):
        return self._market.discover(query)

    def analyse(self, skill_id):
        return self._market.candidate(skill_id)

    def sandbox_evaluate(self, skill_id):
        return self._market.evaluate(skill_id)

    def compare(self, skill_id):
        return self._market.compare(skill_id)

    def recommend(self, query, role="coder"):
        """Recommend installed skills first; otherwise name candidates that
        a HUMAN could quarantine/approve."""
        installed = self._market.registry.select(role, query, limit=5)
        discovery = self._market.discover(query)
        return {"installed": [s["id"] for s in installed],
                "candidates": [c["id"] for c in discovery["candidates"]],
                "note": "candidates require explicit quarantine, "
                        "evaluation and human approval before use"}


# -- helpers ------------------------------------------------------------------------

def _matches(meta, query_low):
    if not query_low:
        return True
    haystack = " ".join([str(meta.get("id", "")), str(meta.get("name", "")),
                         str(meta.get("description", "")),
                         " ".join(map(str, meta.get("triggers") or []))]) \
        .lower()
    return any(term in haystack for term in query_low.split())


def _read_source(source):
    """Yield candidate metadata dicts from one configured registry."""
    stype = source.get("type")
    path = source.get("path")
    if not path or not os.path.exists(path):
        return []
    out = []
    if stype in ("local_index", "internal", "mirror_index"):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (ValueError, OSError):
            return []
        for entry in data.get("skills", []):
            if entry.get("id"):
                out.append(dict(entry))
    elif stype in ("directory", "local", "git", "mirror"):
        for name in sorted(os.listdir(path)):
            directory = os.path.join(path, name)
            if not os.path.isdir(directory):
                continue
            meta = {"id": name, "local_path": directory}
            manifest = None
            for candidate in ("skill.yaml", "skill.yml", "manifest.yaml"):
                if os.path.exists(os.path.join(directory, candidate)):
                    manifest = os.path.join(directory, candidate)
                    break
            if manifest:
                try:
                    with open(manifest, encoding="utf-8") as fh:
                        meta.update(yaml.safe_load(fh) or {})
                except (yaml.YAMLError, OSError):
                    pass
            meta.setdefault("id", name)
            meta.setdefault("revision", _dir_revision(directory))
            meta["local_path"] = directory
            out.append(meta)
    return out


def _dir_revision(directory):
    """Deterministic pseudo-revision for a plain directory source: content
    hash. A git mirror should advertise its commit instead."""
    return "dir-" + SkillRegistry.compute_checksum(directory)[:12]


def _injection_findings(directory):
    findings = []
    for base, _dirs, files in os.walk(directory):
        for name in files:
            if not name.lower().endswith((".md", ".txt", ".yaml", ".yml")):
                continue
            try:
                with open(os.path.join(base, name), encoding="utf-8",
                          errors="replace") as fh:
                    content = fh.read(MAX_INSTRUCTION_BYTES)
            except OSError:
                continue
            for match in INJECTION_RE.finditer(content):
                findings.append("%s: %r" % (name, match.group(0)[:60]))
    return findings[:10]


def _instruction_file(directory):
    for name in ("SKILL.md", "skill.md", "README.md"):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return path
    return None


def _read_licence(directory):
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    return fh.readline().strip()[:100] or "present"
            except OSError:
                pass
    return "unknown"


def _file_hashes(directory):
    out = {}
    for base, _dirs, files in os.walk(directory):
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, directory).replace("\\", "/")
            with open(full, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out
