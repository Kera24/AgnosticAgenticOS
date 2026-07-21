"""Phase 2 -- Capability Taxonomy: loading, schema validation, referential
integrity, custom org/project extension, and lookup helpers. Pure data +
deterministic checks -- no I/O beyond reading YAML, no model calls."""
import os

import pytest
import yaml

from conftest import AGENTIC_SRC
from core.capability import Taxonomy, TaxonomyError, load_taxonomy
from core.schema import load_schema


def test_builtin_taxonomy_loads_and_strictly_validates():
    t = load_taxonomy(agentic_dir=AGENTIC_SRC, strict=True)
    assert t.taxonomy_version == 1
    assert len(t.capabilities) >= 40
    assert len(t.categories) == 39
    assert t.validate(load_schema(str(
        AGENTIC_SRC / "schemas" / "capability-definition.schema.json"))) == []


def test_every_declared_category_is_a_string_and_deduplicated():
    t = load_taxonomy(agentic_dir=AGENTIC_SRC)
    assert len(t.categories) == len(set(t.categories))
    assert "SEO" in t.categories and "Supabase" in t.categories


# -- the two worked examples from the design spec -----------------------------------

def test_hotel_seo_example_direct_triggers_and_dependencies():
    t = load_taxonomy(agentic_dir=AGENTIC_SRC, strict=True)
    text = ("The hotel website must rank for boutique hotel searches in "
           "Kathmandu.")
    direct = {c["id"] for c in t.matching_triggers(text)}
    assert "technical_seo" in direct
    # technical_seo pulls in the granular SEO mechanics as dependencies
    assert set(t.dependencies_of("technical_seo")) == \
        {"sitemap", "robots", "metadata"}
    assert set(t.dependencies_of("performance")) == {"image_optimisation"}
    assert set(t.dependencies_of("local_seo")) >= {"structured_data"}


def test_supabase_auth_example_direct_triggers_and_dependencies():
    t = load_taxonomy(agentic_dir=AGENTIC_SRC, strict=True)
    text = "Use Supabase for authentication and the database."
    direct = {c["id"] for c in t.matching_triggers(text)}
    assert direct >= {"supabase", "authentication", "relational_database"}
    assert "database_migrations" in t.dependencies_of("supabase")
    # row-level security becomes mandatory once supabase is selected --
    # the mandatory_conditions data supports this even without an
    # explicit "multi-tenant" phrase (Phase 3 evaluates the predicate)
    rls = t.get("row_level_security")
    assert {"capability_present": "supabase"} in rls["mandatory_conditions"]
    assert "authorisation" in t.get("supabase")["dependencies"] or \
        "authorisation" in t.get("authentication")["dependencies"] or \
        {"capability_present": "authentication"} in \
        t.get("authorisation")["mandatory_conditions"]


# -- capability IDs are never coupled to a specific skill --------------------------

def test_suggested_skills_are_hints_not_hard_bindings():
    t = load_taxonomy(agentic_dir=AGENTIC_SRC)
    for cap in t.capabilities.values():
        # every suggestion field is a plain list of strings (names), never
        # a structural reference the loader resolves/requires to exist
        for field in ("suggested_skills", "suggested_mcp_capabilities",
                      "suggested_plugins", "deterministic_tools"):
            assert isinstance(cap.get(field, []), list)


# -- referential integrity / conflict detection -------------------------------------

def _taxonomy_from(capabilities, categories=None):
    return Taxonomy(1, categories or ["testcat"], {c["id"]: c
                                                    for c in capabilities})


def _cap(id_, **kw):
    base = {"id": id_, "name": id_, "category": "testcat",
           "description": "d", "risk_level": "low", "version": 1,
           "dependencies": [], "alternatives": [], "conflicts_with": []}
    base.update(kw)
    return base


def test_validate_flags_unknown_dependency_reference():
    t = _taxonomy_from([_cap("a", dependencies=["missing"])])
    violations = t.validate()
    assert any("unknown capability" in v for v in violations)


def test_validate_flags_self_reference():
    t = _taxonomy_from([_cap("a", dependencies=["a"])])
    violations = t.validate()
    assert any("references itself" in v for v in violations)


def test_validate_flags_dependency_cycle():
    t = _taxonomy_from([_cap("a", dependencies=["b"]),
                        _cap("b", dependencies=["c"]),
                        _cap("c", dependencies=["a"])])
    violations = t.validate()
    assert any("dependency cycle" in v for v in violations)


def test_validate_flags_depends_on_and_conflicts_with_same_target():
    t = _taxonomy_from([_cap("a", dependencies=["b"],
                             conflicts_with=["b"]),
                        _cap("b")])
    violations = t.validate()
    assert any("depends on and conflicts with" in v for v in violations)


def test_validate_flags_undeclared_category():
    t = _taxonomy_from([_cap("a", category="not_declared")])
    violations = t.validate()
    assert any("not declared" in v for v in violations)


def test_validate_clean_taxonomy_has_no_violations():
    t = _taxonomy_from([_cap("a", dependencies=["b"]), _cap("b")])
    assert t.validate() == []


def test_conflicts_of_is_symmetric():
    t = _taxonomy_from([_cap("a", conflicts_with=["b"]), _cap("b")])
    assert t.conflicts_of("a") == {"b"}
    assert t.conflicts_of("b") == {"a"}   # b never declared it, but a did


def test_schema_validation_catches_missing_required_field(tmp_path):
    schema = load_schema(str(AGENTIC_SRC / "schemas" /
                             "capability-definition.schema.json"))
    bad = _cap("a")
    del bad["risk_level"]
    t = _taxonomy_from([bad])
    violations = t.validate(schema)
    assert any("missing required property" in v for v in violations)


# -- custom org/project capability extension (requirement 2) -----------------------

def test_org_override_directory_merges_and_overrides_builtin(tmp_path):
    agentic_dir = tmp_path / "agentic"
    caps_dir = agentic_dir / "capabilities"
    org_dir = caps_dir / "org"
    org_dir.mkdir(parents=True)
    (caps_dir / "taxonomy.yaml").write_text(yaml.safe_dump({
        "taxonomy_version": 1,
        "categories": ["testcat"],
        "capabilities": [_cap("builtin_only"), _cap("overridden",
                                                    name="Original")],
    }), encoding="utf-8")
    (org_dir / "extra.yaml").write_text(yaml.safe_dump({
        "categories": ["org_extra"],
        "capabilities": [_cap("org_addition", category="org_extra"),
                         _cap("overridden", name="Org Renamed")],
    }), encoding="utf-8")
    t = load_taxonomy(agentic_dir=agentic_dir, strict=True)
    assert set(t.capabilities) == {"builtin_only", "overridden",
                                   "org_addition"}
    assert t.get("overridden")["name"] == "Org Renamed"   # org wins
    assert "org_extra" in t.categories   # org can extend categories


def test_load_taxonomy_strict_raises_on_broken_data(tmp_path):
    agentic_dir = tmp_path / "agentic"
    (agentic_dir / "capabilities").mkdir(parents=True)
    (agentic_dir / "capabilities" / "taxonomy.yaml").write_text(
        yaml.safe_dump({
            "taxonomy_version": 1, "categories": ["testcat"],
            "capabilities": [_cap("a", dependencies=["missing"])],
        }), encoding="utf-8")
    with pytest.raises(TaxonomyError):
        load_taxonomy(agentic_dir=agentic_dir, strict=True)


def test_load_taxonomy_non_strict_returns_taxonomy_despite_violations(
        tmp_path):
    agentic_dir = tmp_path / "agentic"
    (agentic_dir / "capabilities").mkdir(parents=True)
    (agentic_dir / "capabilities" / "taxonomy.yaml").write_text(
        yaml.safe_dump({
            "taxonomy_version": 1, "categories": ["testcat"],
            "capabilities": [_cap("a", dependencies=["missing"])],
        }), encoding="utf-8")
    t = load_taxonomy(agentic_dir=agentic_dir, strict=False)
    assert t.get("a") is not None
    assert t.validate() != []


def test_missing_builtin_taxonomy_raises(tmp_path):
    with pytest.raises(TaxonomyError):
        load_taxonomy(agentic_dir=tmp_path / "nowhere")


def test_capability_definition_missing_id_raises(tmp_path):
    agentic_dir = tmp_path / "agentic"
    (agentic_dir / "capabilities").mkdir(parents=True)
    (agentic_dir / "capabilities" / "taxonomy.yaml").write_text(
        yaml.safe_dump({"taxonomy_version": 1, "categories": [],
                        "capabilities": [{"name": "no id"}]}),
        encoding="utf-8")
    with pytest.raises(TaxonomyError):
        load_taxonomy(agentic_dir=agentic_dir)


# -- lookup helpers -------------------------------------------------------------------

def test_by_category_and_by_project_type():
    t = load_taxonomy(agentic_dir=AGENTIC_SRC)
    seo_caps = {c["id"] for c in t.by_category("SEO")}
    assert seo_caps == {"technical_seo", "local_seo", "sitemap", "robots",
                        "metadata"}
    static_caps = {c["id"] for c in t.by_project_type("static_site")}
    assert "technical_seo" in static_caps
    assert "backend_api" not in static_caps   # scoped away from static sites


# -- doctor integration ---------------------------------------------------------------

def test_doctor_reports_capability_taxonomy_ok(sandbox):
    from core.doctor import run_doctor
    ok, checks = run_doctor(cfg=sandbox["cfg"])
    line = next(msg for level, msg in checks
               if msg.startswith("capability taxonomy"))
    assert line.startswith("capability taxonomy: version 1, 48")
    assert not any(level == "error" and "capability taxonomy" in msg
                  for level, msg in checks)


def test_doctor_warns_not_errors_when_taxonomy_directory_absent(
        tmp_path, monkeypatch, base_cfg):
    import core.doctor as doctor_mod
    agentic = tmp_path / "agentic_no_capabilities"
    for sub in ("prompts", "schemas", "guardrails"):
        import shutil
        shutil.copytree(AGENTIC_SRC / sub, agentic / sub)
    # doctor.py binds AGENTIC_DIR at import time (`from .config import
    # AGENTIC_DIR`); patch doctor's own name, matching how doctor actually
    # resolves it, rather than core.config's (which doctor won't re-read).
    monkeypatch.setattr(doctor_mod, "AGENTIC_DIR", agentic)
    cfg = dict(base_cfg)
    cfg["project"]["repository_root"] = str(tmp_path)
    ok, checks = doctor_mod.run_doctor(cfg=cfg)
    taxonomy_lines = [(level, msg) for level, msg in checks
                      if msg.startswith("capability taxonomy")]
    assert taxonomy_lines and taxonomy_lines[0][0] == "warn"
