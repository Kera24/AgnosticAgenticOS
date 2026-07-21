"""Capability Taxonomy (Phase 2): loads, validates, and indexes the
declarative CapabilityDefinition catalogue.

Provider-neutral by construction: no field couples a capability id to a
specific skill/MCP server/plugin -- `suggested_*` fields are hints only,
consumed (or not) by the Capability Resolver (Phase 5). The taxonomy
itself never calls a model and never touches the network; it is pure
data plus deterministic validation.

Custom organisation/project capabilities: any `.yaml`/`.yml` file under
`.agentic/capabilities/org/` is merged on top of the built-in
`taxonomy.yaml` (same shape). An org file may add brand-new capability
ids, override a built-in id's definition entirely (last file wins, files
merged in sorted filename order), and/or extend the declared `categories`
list. Nothing here mutates the built-in file.
"""
import os

import yaml

from .. import errors
from ..schema import load_schema, validate as schema_validate

_DEFINITION_SCHEMA_NAME = "capability-definition.schema.json"


class TaxonomyError(errors.PolicyError):
    """Taxonomy data failed to load or (in strict mode) failed
    validation."""


def _load_yaml_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise TaxonomyError("%s: expected a mapping at the top level"
                            % path)
    return data


def _default_paths(agentic_dir):
    base = os.path.join(str(agentic_dir), "capabilities")
    built_in = os.path.join(base, "taxonomy.yaml")
    org_dir = os.path.join(base, "org")
    org_files = []
    if os.path.isdir(org_dir):
        org_files = sorted(
            os.path.join(org_dir, f) for f in os.listdir(org_dir)
            if f.endswith((".yaml", ".yml")))
    return built_in, org_files


class Taxonomy:
    """Loaded, indexed capability catalogue."""

    def __init__(self, taxonomy_version, categories, capabilities):
        self.taxonomy_version = taxonomy_version
        self.categories = list(dict.fromkeys(categories))   # dedupe, keep order
        self.capabilities = capabilities   # id -> definition dict

    # -- lookups ------------------------------------------------------------
    def get(self, capability_id):
        return self.capabilities.get(capability_id)

    def by_category(self, category):
        return [c for c in self.capabilities.values()
               if c.get("category") == category]

    def by_project_type(self, project_type):
        """Capabilities that either declare no project_types restriction
        (applicable to anything) or explicitly list this one."""
        return [c for c in self.capabilities.values()
               if not c.get("project_types")
               or project_type in c["project_types"]]

    def matching_triggers(self, text):
        """Capabilities whose `triggers` list contains a literal
        substring of `text` (case-insensitive). A pure lookup helper --
        the Requirements Intelligence Engine (Phase 3) does the real
        deterministic-rules-first matching this feeds."""
        low = (text or "").lower()
        return [c for c in self.capabilities.values()
               if any(t.lower() in low for t in c.get("triggers") or [])]

    def dependencies_of(self, capability_id):
        c = self.get(capability_id)
        return list((c or {}).get("dependencies") or [])

    def conflicts_of(self, capability_id):
        """Symmetric conflict set: explicit `conflicts_with` plus any
        capability that names this one as a conflict (data authors only
        need to declare a conflict from one side)."""
        c = self.get(capability_id)
        direct = set((c or {}).get("conflicts_with") or [])
        reverse = {other_id for other_id, other in self.capabilities.items()
                  if capability_id in (other.get("conflicts_with") or [])}
        return direct | reverse

    # -- validation -----------------------------------------------------------
    def validate(self, definition_schema=None):
        """Returns a list of human-readable violations; empty == valid.
        Never raises -- callers (doctor, `load_taxonomy(strict=True)`,
        tests) decide what to do with the result."""
        violations = []
        ids = set(self.capabilities)
        for cap_id, cap in self.capabilities.items():
            if definition_schema is not None:
                for v in schema_validate(cap, definition_schema):
                    violations.append("%s: %s" % (cap_id, v))
            if cap.get("category") not in self.categories:
                violations.append(
                    "%s: category %r is not declared in categories"
                    % (cap_id, cap.get("category")))
            for field in ("dependencies", "alternatives", "conflicts_with"):
                for ref in cap.get(field) or []:
                    if ref == cap_id:
                        violations.append(
                            "%s: %s references itself" % (cap_id, field))
                    elif ref not in ids:
                        violations.append(
                            "%s: %s references unknown capability %r"
                            % (cap_id, field, ref))
            deps = set(cap.get("dependencies") or [])
            conflicts = set(cap.get("conflicts_with") or [])
            contradictory = deps & conflicts
            if contradictory:
                violations.append(
                    "%s: depends on and conflicts with %s"
                    % (cap_id, sorted(contradictory)))
        violations.extend(self._cycle_violations())
        return violations

    def _cycle_violations(self):
        """DFS cycle detection over the dependency graph."""
        WHITE, GREY, BLACK = 0, 1, 2
        colour = {cap_id: WHITE for cap_id in self.capabilities}
        violations = []

        def visit(cap_id, path):
            colour[cap_id] = GREY
            for dep in self.capabilities.get(cap_id, {}).get(
                    "dependencies") or []:
                if dep not in colour:
                    continue   # unknown ref already reported separately
                if colour[dep] == GREY:
                    cycle = path[path.index(dep):] + [dep]
                    violations.append(
                        "dependency cycle: %s" % " -> ".join(cycle))
                elif colour[dep] == WHITE:
                    visit(dep, path + [dep])
            colour[cap_id] = BLACK

        for cap_id in self.capabilities:
            if colour[cap_id] == WHITE:
                visit(cap_id, [cap_id])
        return violations


def _merge_taxonomy_sources(sources):
    """sources: list of raw dicts (already yaml-loaded), built-in first,
    org overrides after, in filename order. Later capability ids override
    earlier ones entirely (no deep merge of a single definition)."""
    version = None
    categories = []
    capabilities = {}
    for data in sources:
        if data.get("taxonomy_version") is not None:
            version = data["taxonomy_version"]
        categories.extend(data.get("categories") or [])
        for cap in data.get("capabilities") or []:
            cap_id = cap.get("id")
            if not cap_id:
                raise TaxonomyError("capability definition missing 'id': %r"
                                    % cap)
            capabilities[cap_id] = cap
    return version or 1, categories, capabilities


def load_taxonomy(agentic_dir=None, extra_paths=None, strict=False):
    """Load the built-in taxonomy plus any org overrides (and any
    explicit `extra_paths`, e.g. a project-specific addition), merge, and
    return a `Taxonomy`. With `strict=True`, raises `TaxonomyError` if
    `.validate()` finds anything wrong -- built-in/org data is platform
    state, not untrusted user input, so failing fast is appropriate for
    callers that need a working taxonomy to proceed."""
    if agentic_dir is None:
        from ..config import AGENTIC_DIR
        agentic_dir = AGENTIC_DIR
    built_in, org_files = _default_paths(agentic_dir)
    paths = [built_in] + org_files + list(extra_paths or [])
    sources = []
    for path in paths:
        if os.path.exists(path):
            sources.append(_load_yaml_file(path))
        elif path == built_in:
            raise TaxonomyError("built-in taxonomy not found: %s" % path)
    version, categories, capabilities = _merge_taxonomy_sources(sources)
    taxonomy = Taxonomy(version, categories, capabilities)

    definition_schema = None
    schema_path = os.path.join(str(agentic_dir), "schemas",
                               _DEFINITION_SCHEMA_NAME)
    if os.path.exists(schema_path):
        definition_schema = load_schema(schema_path)

    if strict:
        violations = taxonomy.validate(definition_schema)
        if violations:
            raise TaxonomyError(
                "capability taxonomy failed validation:\n  "
                + "\n  ".join(violations[:20]))
    return taxonomy
