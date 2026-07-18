"""MP Phase 7 — Docker/Supabase adapters: compose-name isolation, unsafe
command rejection, migration-first rule, environment policy, evidence."""
import json
import os

import pytest

from conftest import Clock, FakeRunner
from core import errors
from core.dockerx import DockerAdapter
from core.supabasex import (SupabaseAdapter, environment_policy, guard)


def docker(tmp_path, project_id="shop", runner=None, cfg=None,
           compose=True):
    root = tmp_path / project_id
    root.mkdir(exist_ok=True)
    if compose:
        (root / "docker-compose.yml").write_text(
            "services:\n  web:\n    image: nginx\n", encoding="utf-8")
    return DockerAdapter(cfg or {}, project_id, str(root),
                         runner=runner or FakeRunner([]),
                         home=str(tmp_path / "home"))


# -- docker -------------------------------------------------------------------------

def test_compose_project_name_isolation(tmp_path):
    runner = FakeRunner([])
    adapter = docker(tmp_path, "shop", runner)
    adapter.compose("ps")
    argv = runner.calls[0]["argv"]
    assert argv[:4] == ["docker", "compose", "--project-name",
                        "agentic-shop"]
    other = docker(tmp_path, "blog", runner)
    other.compose("up")
    assert "agentic-blog" in runner.calls[1]["argv"]
    assert "-d" in runner.calls[1]["argv"]        # detached by default


def test_unsafe_operations_rejected(tmp_path):
    adapter = docker(tmp_path)
    with pytest.raises(errors.PolicyError, match="not allowed"):
        adapter.compose("rm")
    with pytest.raises(errors.PolicyError, match="not allowed"):
        adapter.compose("push")
    with pytest.raises(errors.PolicyError, match="denied"):
        adapter.compose("up", "--privileged")
    with pytest.raises(errors.PolicyError, match="denied"):
        adapter.compose("down", "prune")
    with pytest.raises(errors.PolicyError, match="socket|denied"):
        adapter.compose("up", "-v", "/var/run/docker.sock:/sock")
    with pytest.raises(errors.PolicyError, match="host-root"):
        adapter.compose("up", "--volume", "/:/host")
    with pytest.raises(errors.PolicyError, match="0.0.0.0"):
        adapter.compose("up", "-p", "0.0.0.0:8080:80")


def test_exec_only_against_approved_services(tmp_path):
    cfg = {"docker": {"approved_exec_services": ["web"]}}
    runner = FakeRunner([])
    adapter = docker(tmp_path, runner=runner, cfg=cfg)
    with pytest.raises(errors.PolicyError, match="approved services"):
        adapter.compose("exec", "db", "psql")
    adapter.compose("exec", "web", "ls")
    assert "web" in runner.calls[0]["argv"]


def test_allowed_operations_configurable(tmp_path):
    cfg = {"docker": {"allowed_operations": ["ps", "logs"]}}
    adapter = docker(tmp_path, cfg=cfg)
    with pytest.raises(errors.PolicyError, match="not allowed"):
        adapter.compose("up")
    adapter.compose("ps")


def test_missing_compose_file(tmp_path):
    adapter = docker(tmp_path, "bare", compose=False)
    with pytest.raises(errors.PolicyError, match="compose file"):
        adapter.compose("up")


def test_build_lock_exclusive(tmp_path):
    runner = FakeRunner([])
    a = docker(tmp_path, "one", runner)
    b = docker(tmp_path, "two", runner)
    with a.build_lock():
        from core.registry import RegistryError
        with pytest.raises(RegistryError, match="lock"):
            with b.build_lock():
                pass
    with b.build_lock():                     # released -> acquirable
        pass


# -- supabase policy -----------------------------------------------------------------

def test_environment_policy_defaults_and_overrides():
    assert environment_policy({}, "local", "reset") == "automatic"
    assert environment_policy({}, "staging", "reset") == "denied"
    assert environment_policy({}, "production",
                              "database_mutation") == "approval_required"
    cfg = {"supabase": {"environments": {
        "development": {"reset": "denied"}}}}
    assert environment_policy(cfg, "development", "reset") == "denied"
    assert environment_policy(cfg, "development",
                              "database_mutation") == "allowed"


def test_guard_raises_and_flags():
    with pytest.raises(errors.PolicyError, match="DENIED"):
        guard({}, "production", "reset")
    with pytest.raises(errors.PolicyError, match="restricted"):
        guard({}, "development", "reset")
    assert guard({}, "staging", "database_mutation") is True
    assert guard({}, "local", "database_mutation") is False


# -- supabase workflows ---------------------------------------------------------------

def supa(tmp_path, runner, migrations=("20260701000000_init.sql",),
         linked=None, seed=False, cfg=None):
    root = tmp_path / "app"
    (root / "supabase" / "migrations").mkdir(parents=True, exist_ok=True)
    (root / "supabase" / "config.toml").write_text("[api]\n",
                                                   encoding="utf-8")
    for name in migrations:
        (root / "supabase" / "migrations" / name).write_text(
            "create table t (id int);\n", encoding="utf-8")
    if seed:
        (root / "supabase" / "seed.sql").write_text("insert into t "
                                                    "values (1);\n",
                                                    encoding="utf-8")
    if linked:
        temp = root / "supabase" / ".temp"
        temp.mkdir(exist_ok=True)
        (temp / "project-ref").write_text(linked, encoding="utf-8")
    return SupabaseAdapter(cfg or {}, "app", str(root), runner=runner,
                           evidence_dir=str(tmp_path / "evidence"),
                           clock=Clock())


def ok(stdout=""):
    return {"exit_code": 0, "stdout": stdout}


def test_detection(tmp_path):
    adapter = supa(tmp_path, FakeRunner([]), linked="abcd1234", seed=True)
    found = adapter.detect()
    assert found["config"] and found["migrations_dir"] and found["seed"]
    assert found["linked_project_ref"] == "abcd1234"
    assert found["migrations"] == ["20260701000000_init.sql"]


def test_local_workflow_success(tmp_path):
    runner = FakeRunner([ok("Resetting local database..."),
                         ok("export type Database = {}")])
    adapter = supa(tmp_path, runner, seed=True)
    report = adapter.local_workflow()
    assert report["ok"]
    steps = [s["step"] for s in report["steps"]]
    assert steps == ["db_reset", "seed", "gen_types"]
    assert runner.calls[0]["argv"][:3] == ["supabase", "db", "reset"]
    assert runner.calls[1]["argv"][:3] == ["supabase", "gen", "types"]


def test_broken_migration_stops_workflow(tmp_path):
    runner = FakeRunner([{"exit_code": 1,
                          "stderr": "ERROR: syntax error in migration"}])
    adapter = supa(tmp_path, runner)
    report = adapter.local_workflow()
    assert not report["ok"]
    assert report["failed_step"] == "db_reset"
    assert len(runner.calls) == 1            # nothing after the failure


def test_migration_first_rule(tmp_path):
    adapter = supa(tmp_path, FakeRunner([]), migrations=())
    with pytest.raises(errors.PolicyError, match="migrations first"):
        adapter.local_workflow()
    with pytest.raises(errors.PolicyError, match="migrations first"):
        adapter.remote_apply("development")


def test_remote_dry_run_and_approval_gate(tmp_path):
    runner = FakeRunner([ok("history"), ok("would apply 1 migration"),
                         ok("history"), ok("applied"), ok("history")])
    adapter = supa(tmp_path, runner, linked="ref9999")
    result = adapter.remote_apply("staging")
    assert result["status"] == "approval_required"
    evidence_files = os.listdir(str(tmp_path / "evidence"))
    assert any("remote-apply" in name for name in evidence_files)
    evidence = json.load(open(os.path.join(str(tmp_path / "evidence"),
                                           evidence_files[0]),
                              encoding="utf-8"))
    assert evidence["dry_run"]["exit_code"] == 0
    # dry-run argv was actually used
    assert any("--dry-run" in c["argv"] for c in runner.calls)
    # with explicit approval it applies and verifies
    result2 = adapter.remote_apply("staging", approved=True)
    assert result2["status"] == "applied"
    assert result2["verified_history"]["exit_code"] == 0


def test_failed_dry_run_refuses_apply(tmp_path):
    runner = FakeRunner([ok("history"),
                         {"exit_code": 1, "stderr": "conflict"}])
    adapter = supa(tmp_path, runner, linked="ref9999")
    with pytest.raises(errors.PolicyError, match="dry run failed"):
        adapter.remote_apply("development")
    assert not any("push\x00" in " ".join(c["argv"])
                   for c in runner.calls)
    # no non-dry push happened
    pushes = [c for c in runner.calls
              if c["argv"][:3] == ["supabase", "db", "push"]
              and "--dry-run" not in c["argv"]]
    assert pushes == []


def test_production_mutation_and_reset_denied(tmp_path):
    adapter = supa(tmp_path, FakeRunner([]), linked="ref9999")
    with pytest.raises(errors.PolicyError, match="DENIED"):
        adapter.remote_reset("production")
    with pytest.raises(errors.PolicyError, match="DENIED"):
        adapter.remote_reset("staging")
    with pytest.raises(errors.PolicyError, match="DENIED"):
        guard({}, "production", "seed")


def test_unlinked_remote_apply_refused(tmp_path):
    adapter = supa(tmp_path, FakeRunner([ok()]))
    with pytest.raises(errors.PolicyError, match="link"):
        adapter.remote_apply("development")


def test_mutation_lock_per_project(tmp_path):
    adapter = supa(tmp_path, FakeRunner([]))
    runtime = str(tmp_path / "runtime")
    with adapter.mutation_lock(runtime):
        from core.registry import RegistryError
        with pytest.raises(RegistryError):
            with adapter.mutation_lock(runtime):
                pass
