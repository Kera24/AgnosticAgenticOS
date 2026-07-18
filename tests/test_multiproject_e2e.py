"""MP Phase 12 — four-project mocked end-to-end scenario.

React+Supabase, Python+Docker, full-stack Claude, and an Ollama-Qwen
fallback project: registered as external folders, initialised, queued,
fleet-scheduled under slot limits, one real cycle built and integrated,
cooling releases slots, docker/supabase stay project-scoped, the skill
curator quarantines without installing, MCP flows through the gateway,
Claude auth reports correctly, the Qwen CLI stays excluded while Ollama
Qwen serves, and a process restart recovers registry/slots/leases. No
provider, docker daemon, database or network is ever touched.
"""
import json
import os
import subprocess

import pytest

from conftest import Clock, FakeRunner
from core import errors, fleet, gitops, projectops, projstate
from core.registry import ProjectRegistry


class ScriptedAdapter:
    backend_type = "api"

    def __init__(self, name, script, calls):
        self.name = name
        self.script = script
        self.calls = calls

    def invoke(self, role, prompt, input_data, workspace, permissions,
               timeout):
        self.calls.append({"backend": self.name, "role": role})
        outputs = self.script.get(role)
        assert outputs, "no script for role %r" % role
        item = outputs.pop(0) if len(outputs) > 1 else outputs[0]
        return {"ok": True, "backend": self.name, "backend_type": "api",
                "model": "scripted", "role": role, "provider": self.name,
                "content": json.dumps(item), "structured_output": {},
                "usage": {"input_tokens": 400, "cached_input_tokens": 0,
                          "output_tokens": 150, "reasoning_tokens": None,
                          "estimated": False},
                "capacity": {"remaining_reported": None, "reset_at": None,
                             "retry_after_seconds": None},
                "finish_reason": "completed", "refusal": False,
                "exit_code": 0, "estimated_cost_usd": 0.0, "error": None}


def architect_out(task_id):
    return {"architecture": "app", "assumptions": [],
            "milestones": [{"id": "m1", "title": "core"}],
            "backlog": [{"id": task_id, "milestone": "m1",
                         "description": "implement the core feature",
                         "dependencies": [], "risk": "low",
                         "security_relevant": False,
                         "expected_paths": ["src/**"],
                         "expected_size": "small",
                         "acceptance_criteria": ["check passes"],
                         "deterministic_checks": [], "skill": "app"}],
            "requirements_map": [], "completion_criteria": ["works"],
            "human_decisions": []}


def order_out():
    return {"action": "execute", "item": "implement core", "skill": "app",
            "spec": "set VALUE to 2",
            "done_when": [{"id": "DW-1", "condition": "check passes",
                           "command": None}],
            "allowed_paths": ["src/**"], "forbidden_paths": [],
            "maximum_changed_lines": 50, "risk": "low",
            "queue_reason": None}


def qa_pass():
    return {"verdict": "pass",
            "done_when_results": [{"id": "DW-1", "passed": True,
                                   "evidence": ["diff"]}],
            "out_of_scope_changes": [], "test_integrity_preserved": True,
            "reason": "ok"}


PROJECT_SPECS = [
    ("react-supabase", {"supabase": True}),
    ("python-docker", {"docker": True}),
    ("fullstack-claude", {}),
    ("qwen-fallback", {}),
]


@pytest.fixture
def world(tmp_path, base_cfg, monkeypatch):
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    registry = ProjectRegistry(home=str(tmp_path / "home"))
    cfg = dict(base_cfg)
    cfg["backends"] = {
        "claude": {"type": "api", "provider": "mock", "model": "m"},
        "codex": {"type": "api", "provider": "mock", "model": "m"},
        "qwen": {"type": "cli", "kind": "configured", "binary": "qwen"},
        "ollama": {"type": "local", "model": "qwen3.5:latest",
                   "cost_free": True},
    }
    cfg["routing"] = {"mode": "simple", "primary": "claude",
                      "fallbacks": ["codex"]}
    cfg["concurrency"] = {"maximum_active_projects": 4,
                          "maximum_model_calls": 2,
                          "per_backend": {"claude": 1, "codex": 1,
                                          "ollama": 1, "qwen": 1}}
    cfg["verification"] = {"commands": [
        {"name": "value-check",
         "command": ("python -c \"import sys; "
                     "sys.exit(0 if open('src/app.py').read()"
                     ".startswith('VALUE = 2') else 1)\""),
         "mandatory": True}], "fail_fast": True}
    cfg["notifications"] = {"desktop": False}

    ids = []
    for name, features in PROJECT_SPECS:
        root = tmp_path / "apps" / name
        (root / "src").mkdir(parents=True)
        (root / "plan.md").write_text("# %s plan\n\nBuild it.\n" % name,
                                      encoding="utf-8")
        (root / "src" / "app.py").write_text("VALUE = 1\n",
                                             encoding="utf-8")
        if features.get("supabase"):
            (root / "supabase" / "migrations").mkdir(parents=True)
            (root / "supabase" / "config.toml").write_text(
                "[api]\n", encoding="utf-8")
            (root / "supabase" / "migrations" /
             "20260701000000_init.sql").write_text(
                "create table t (id int);\n", encoding="utf-8")
        if features.get("docker"):
            (root / "docker-compose.yml").write_text(
                "services:\n  web:\n    image: nginx\n", encoding="utf-8")
        record = registry.add(name, str(root))
        ids.append(record["id"])
    return {"registry": registry, "cfg": cfg, "ids": ids,
            "home": registry.home, "clock": Clock(),
            "tmp": tmp_path}


def scripted(monkeypatch, script, calls):
    from core import project as project_mod
    monkeypatch.setattr(
        project_mod.backends, "build_backend",
        lambda cfg_, name, **kw: ScriptedAdapter(name, script, calls))


def route_to(world, pid, primary, fallbacks=()):
    world["registry"].update(pid, backend_profile={
        "routing": {"mode": "simple", "primary": primary,
                    "fallbacks": list(fallbacks)}})


def test_four_project_scenario(world, monkeypatch):
    registry, cfg, clock = world["registry"], world["cfg"], world["clock"]
    home = world["home"]
    calls = []

    # 1–2. register (done in fixture) + init: git, runtime state, index,
    # memory, vault — all model-free and isolated
    for pid in world["ids"]:
        result = projectops.project_init(cfg, registry, pid)
        assert result["ok"], result
    react, docker_proj, claude_proj, qwen_proj = world["ids"]
    assert registry.get(react)["supabase_project_ref"] is None
    assert projectops.detect_integrations(
        registry.get(react)["root_path"])["supabase"]
    assert projectops.detect_integrations(
        registry.get(docker_proj)["root_path"])["docker"]

    # 3. parse all four plans through the real architect path
    script = {"architect": [architect_out("t1-core")]}
    scripted(monkeypatch, script, calls)
    from core.project import project_start, run_cycle
    for pid in world["ids"]:
        script["architect"] = [architect_out("t1-core")]
        proj_cfg = projectops.project_cfg_for(cfg, registry,
                                              registry.get(pid))
        started = project_start(proj_cfg,
                                projectops.find_plan(registry.get(pid)),
                                clock=clock)
        assert started["status"] == "started", (pid, started)
        registry.update(pid, enabled=True)

    # 16–17. Qwen CLI unverified -> excluded; Ollama Qwen is the fallback
    from core.routing import capability_chain
    qwen_cfg = projectops.project_cfg_for(cfg, registry,
                                          registry.get(qwen_proj))
    qwen_cfg["routing"] = {"mode": "capability", "agents": {}}
    qwen_cfg["backends"] = {k: v for k, v in cfg["backends"].items()
                            if k in ("qwen", "ollama")}
    chain = capability_chain(qwen_cfg, "coder",
                             memory_dir=os.path.join(
                                 registry.project_runtime_dir(qwen_proj),
                                 "memory"))
    assert chain == ["ollama"]
    route_to(world, qwen_proj, "ollama")
    route_to(world, react, "claude")
    route_to(world, docker_proj, "codex")
    route_to(world, claude_proj, "claude")

    # 4–6. queue all; the fleet starts only what the slots permit
    ran = []

    def runner(cfg_, registry_, project_id):
        ran.append(project_id)
        if project_id == claude_proj:
            # 6–7 + 13: one project actually BUILDS through the real
            # engine: coder -> deterministic check -> QA -> security ->
            # local merge integration
            script.update({"conductor": [order_out()],
                           "coder": [{"summary": "edit", "blocked": False,
                                      "blocker": None,
                                      "edits": [{"path": "src/app.py",
                                                 "action": "write",
                                                 "content": "VALUE = 2\n"}],
                                      "commands": []}],
                           "qa": [qa_pass()], "security": [qa_pass()]})
            proj_cfg = projectops.project_cfg_for(cfg_, registry_,
                                                  registry_.get(project_id))
            return run_cycle(proj_cfg, clock=clock)
        # simulated cycle: mark the finished state a real cycle would leave
        from core.scheduler import Scheduler as _S
        proj_cfg = projectops.project_cfg_for(cfg_, registry_,
                                              registry_.get(project_id))
        _S(proj_cfg, os.path.join(
            registry_.project_runtime_dir(project_id), "memory"),
            clock=clock).start_cooling("success")
        return {"status": "success", "simulated": True}

    tick = fleet.run_tick(cfg, registry, runner=runner, clock=clock,
                          home=home)
    assert len(tick["start"]) == 2                    # model slots: 2
    waiting_reasons = {w["project"]: w["reason"] for w in tick["waiting"]}
    assert len(waiting_reasons) >= 2
    assert all("slot" in r or "capacity" in r
               for r in waiting_reasons.values())
    assert sorted(ran) == sorted(s["project"] for s in tick["start"])

    # the real build landed on the project's agentic branch, isolated
    if claude_proj in ran:
        runtime = registry.project_runtime_dir(claude_proj)
        merged = open(os.path.join(runtime, "worktrees", "project", "src",
                                   "app.py")).read()
        assert merged == "VALUE = 2\n"
        # the OTHER projects' repos are untouched
        for pid in (react, docker_proj, qwen_proj):
            source = open(os.path.join(registry.get(pid)["root_path"],
                                       "src", "app.py")).read()
            assert source == "VALUE = 1\n"

    # 7. cooling releases the slot for a waiting project
    from core.scheduler import Scheduler
    for pid in ran:
        state_dir = registry.project_runtime_dir(pid)
        assert Scheduler(projectops.project_cfg_for(cfg, registry,
                                                    registry.get(pid)),
                         os.path.join(state_dir, "memory"),
                         clock=clock).state["state"] == "cooling"
    second = fleet.plan(cfg, registry, clock=clock, home=home)
    started_second = [s["project"] for s in second["start"]]
    assert started_second
    assert not set(started_second) & set(ran)         # cooled ones wait
    for pid in started_second:
        fleet.SlotManager(home, clock=clock).release(pid)

    # 8. one project waits for the docker build slot
    from core.dockerx import DockerAdapter
    from core.registry import RegistryError
    adapter = DockerAdapter(cfg, docker_proj,
                            registry.get(docker_proj)["root_path"],
                            runner=FakeRunner([]), home=home)
    with adapter.build_lock():
        rival = DockerAdapter(cfg, react,
                              registry.get(react)["root_path"],
                              runner=FakeRunner([]), home=home)
        with pytest.raises(RegistryError, match="lock"):
            with rival.build_lock():
                pass
    # docker names are project-scoped
    assert adapter.compose_project == "agentic-python-docker"

    # 9. the Supabase project creates and validates a LOCAL migration
    from core.supabasex import SupabaseAdapter, guard
    supa_runner = FakeRunner([{"exit_code": 0, "stdout": "reset ok"},
                              {"exit_code": 0, "stdout": "types"}])
    supabase = SupabaseAdapter(cfg, react,
                               registry.get(react)["root_path"],
                               runner=supa_runner)
    report = supabase.local_workflow()
    assert report["ok"]
    # 21. production stays untouchable
    with pytest.raises(errors.PolicyError):
        guard(cfg, "production", "database_mutation") and None
        supabase.remote_reset("production")

    # 10–13. missing skill -> curator discovers + quarantines; nothing
    # installs without approval; approved skill loads into later tasks
    from core.skillmarket import SkillCurator, SkillMarket
    mirror = world["tmp"] / "mirror" / "react-patterns"
    mirror.mkdir(parents=True)
    (mirror / "skill.yaml").write_text(
        "id: react-patterns\ndescription: react component patterns\n"
        "license: MIT\ntriggers: [react, component]\n"
        "compatible_agents: [coder]\n", encoding="utf-8")
    (mirror / "SKILL.md").write_text("# react\n\nUse hooks well.\n",
                                     encoding="utf-8")
    cfg["skills"] = {"enabled": True, "registries": [
        {"name": "mirror", "type": "directory",
         "path": str(world["tmp"] / "mirror")}]}
    # isolated skills root — never the live platform installation
    skills_root = world["tmp"] / "skills-root"
    skills_root.mkdir()
    market = SkillMarket(cfg, str(skills_root), home)
    curator = SkillCurator(market)
    recommendation = curator.recommend("react component work")
    assert "react-patterns" in recommendation["candidates"]
    market.quarantine("react-patterns")
    assert market.registry.get("react-patterns") is None   # 12: no install
    market.approve("react-patterns")                       # 13: explicit
    market.registry.enable("react-patterns")
    selected = market.registry.select("coder", "build a react component")
    assert "react-patterns" in [s["id"] for s in selected]

    # 14. a local MCP tool invoked through the gateway
    from core.mcp import MCPGateway

    class Session:
        def __init__(self, argv, timeout=30, env=None):
            pass

        def notify(self, method, params):
            pass

        def request(self, method, params):
            if method == "tools/list":
                return {"tools": [{"name": "list_tables"}]}
            return {"content": [{"type": "text", "text": "tables: t"}]}

        def close(self):
            pass

    gateway = MCPGateway(cfg, home, session_factory=Session)
    server = gateway.add("supabase-local", transport="stdio",
                         command="npx -y @supabase/mcp", scope="project",
                         project_id=react, allowed_tools=["list_tables"])
    gateway.mark_reviewed(server["id"])
    gateway.enable(server["id"])
    result = gateway.call(server["id"], "list_tables", project_id=react)
    assert result["trust"] == "untrusted" and "tables" in result["content"]
    with pytest.raises(Exception):
        gateway.call(server["id"], "list_tables", project_id=docker_proj)

    # 15. Claude authentication reports correctly (scripted status)
    from core.authx import claude_auth_detail, qwen_auth_detail
    claude_report = claude_auth_detail(
        runner=FakeRunner([{"exit_code": 0, "stdout": json.dumps(
            {"loggedIn": True, "authMethod": "subscription"})}]),
        which=lambda b: "claude", env={})
    assert claude_report["state"] == "authenticated"
    qwen_report = qwen_auth_detail(runner=FakeRunner([
        {"exit_code": 0, "stdout": "0.9"}]), which=lambda b: "qwen",
        memory_dir=os.path.join(home, "memory"))
    assert qwen_report["state"] == "unverified"        # 16

    # 18–19. process restart: registry, slots and leases recover
    dead = {"slots": {"model": 1, "backend:claude": 1},
            "backend": "claude", "pid": 999999,
            "machine": fleet._machine(),
            "acquired_at": "2026-07-15T11:00:00",
            "expires_at": "2026-07-15T20:00:00"}
    slots = fleet.SlotManager(home, clock=clock)
    with open(slots.path, "w", encoding="utf-8") as fh:
        json.dump({claude_proj: dead}, fh)
    fresh_registry = ProjectRegistry(home=home)        # "new process"
    assert len(fresh_registry.list()) == 4
    fresh_slots = fleet.SlotManager(home, clock=clock)
    assert fresh_slots.usage()["active_projects"] == 0  # dead pid reaped
    third = fleet.plan(cfg, fresh_registry, clock=clock, home=home)
    assert claude_proj in third["states"]               # schedulable again

    # 20. isolation: separate runtime state everywhere
    runtime_dirs = [registry.project_runtime_dir(pid)
                    for pid in world["ids"]]
    assert len(set(runtime_dirs)) == 4
    for pid in world["ids"]:
        runtime = registry.project_runtime_dir(pid)
        assert os.path.exists(os.path.join(runtime, "memory",
                                           "memory.db"))
        index = json.load(open(os.path.join(runtime, "memory",
                                            "code-index", "state.json"),
                               encoding="utf-8"))
        assert index["provider"] == "native"
    # memories are namespaced per project id
    from core.memsvc import MemoryService
    react_memory = MemoryService(os.path.join(
        registry.project_runtime_dir(react), "memory"), react)
    react_memory.save("constraint", "react only", "isolated fact")
    other_memory = MemoryService(os.path.join(
        registry.project_runtime_dir(docker_proj), "memory"), docker_proj)
    assert other_memory.search("isolated") == []

    # 21. no push/remote/production mutation anywhere
    for pid in world["ids"]:
        remotes = subprocess.run(
            ["git", "remote"], cwd=registry.get(pid)["root_path"],
            capture_output=True, text=True).stdout.strip()
        assert remotes == ""
    pushes = [c for c in supa_runner.calls
              if c["argv"][:3] == ["supabase", "db", "push"]]
    assert pushes == []
