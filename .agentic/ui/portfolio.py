"""Dashboard snapshots and actions for the multi-project command centre:
portfolio, fleet, MCP, backend auth, skills marketplace. Reads assemble
persisted state; mutations are confirmed upstream, validated here, and
never touch application files destructively."""
import os

from core import config as config_mod
from core import fleet as fleet_mod
from core import projectops, projstate
from core.registry import ProjectRegistry


def _registry():
    return ProjectRegistry()


def portfolio_snapshot(cfg):
    registry = _registry()
    projects = []
    for record in registry.list():
        state, reason = fleet_mod.classify_project(cfg, registry, record)
        proj_cfg = projectops.project_cfg_for(cfg, registry, record)
        runtime_dir = proj_cfg["runtime"]["project_dir"]
        progress = projstate.read_yaml(runtime_dir, "progress.yaml", {}) \
            or {}
        from core.scheduler import Scheduler
        scheduler = Scheduler(proj_cfg,
                              os.path.join(runtime_dir, "memory"))
        from core.taskspace import ProjectLease, active_claims
        lease = ProjectLease(runtime_dir, record["id"]).holder()
        integrations = projectops.detect_integrations(record["root_path"]) \
            if os.path.isdir(record["root_path"]) else {}
        index_state = None
        index_path = os.path.join(runtime_dir, "memory", "code-index",
                                  "state.json")
        if os.path.exists(index_path):
            import json
            try:
                index_state = json.load(open(index_path,
                                             encoding="utf-8"))
            except ValueError:
                index_state = None
        projects.append({
            "id": record["id"], "name": record["name"],
            "root_path": record["root_path"],
            "plan_path": record.get("plan_path"),
            "git_repository": record.get("git_repository"),
            "default_branch": record.get("default_branch"),
            "agentic_branch": record.get("agentic_branch"),
            "status": record["status"], "enabled": record["enabled"],
            "priority": record.get("priority"),
            "state": state, "waiting_reason": reason,
            "scheduler": {k: scheduler.state.get(k) for k in
                          ("state", "next_run_at", "cooling_reason",
                           "current_cycle", "selected_backend")},
            "progress": progress.get("tasks_by_status"),
            "milestones": progress.get("milestones"),
            "worktree": os.path.join(runtime_dir, "worktrees", "project"),
            "task_worktrees": sorted(os.listdir(
                os.path.join(runtime_dir, "worktrees", "tasks")))
            if os.path.isdir(os.path.join(runtime_dir, "worktrees",
                                          "tasks")) else [],
            "ownership_claims": active_claims(runtime_dir),
            "lease": {k: lease.get(k) for k in
                      ("machine_id", "pid", "expires_at")}
            if lease else None,
            "docker": {"detected": integrations.get("docker"),
                       "compose_project":
                           record.get("docker_compose_project")},
            "supabase": {"detected": integrations.get("supabase"),
                         "project_ref":
                             record.get("supabase_project_ref")},
            "code_index": index_state,
            "runtime_dir": runtime_dir,
        })
    return {"projects": projects,
            "runtime_home": registry.home,
            "authorised_roots": registry.load().get("authorised_roots",
                                                    [])}


def fleet_snapshot(cfg):
    registry = _registry()
    slots = fleet_mod.SlotManager(registry.home)
    preview = fleet_mod.plan(cfg, registry, home=registry.home, dry=True)
    return {
        "global_pause":
            fleet_mod.load_fleet_state(registry.home)["global_pause"],
        "limits": fleet_mod.concurrency_config(cfg),
        "slots": slots.usage(),
        "would_start": preview["start"],
        "waiting": preview["waiting"],
        "states": preview["states"],
        "recent_decisions": fleet_mod.read_decisions(registry.home,
                                                     limit=5),
    }


def project_action(cfg, project_id, action):
    """Non-destructive lifecycle actions (destructive ones are confirmed
    at the endpoint layer)."""
    registry = _registry()
    from core.scheduler import Scheduler
    record = registry.get(project_id)
    proj_cfg = projectops.project_cfg_for(cfg, registry, record)
    memdir = os.path.join(proj_cfg["runtime"]["project_dir"], "memory")
    if action == "init":
        return projectops.project_init(cfg, registry, project_id)
    if action == "doctor":
        return projectops.project_doctor(cfg, registry, project_id)
    if action == "pause":
        Scheduler(proj_cfg, memdir).pause()
        return {"project": project_id, "paused": True}
    if action == "resume":
        Scheduler(proj_cfg, memdir).resume(force=True)
        registry.update(project_id, enabled=True)
        return {"project": project_id, "resumed": True}
    if action == "stop":
        Scheduler(proj_cfg, memdir).pause()
        registry.update(project_id, enabled=False)
        return {"project": project_id, "stopped": True}
    if action == "enable":
        registry.update(project_id, enabled=True)
        return {"project": project_id, "enabled": True}
    if action == "archive":
        return registry.archive(project_id)
    if action == "remove":
        return registry.remove(project_id)
    raise ValueError("unsupported action %r" % action)


def add_project(cfg, name, root, plan="plan.md", create=False):
    registry = _registry()
    record = registry.add(name, root, plan=plan or "plan.md",
                          create=create)
    if create:
        plan_path = os.path.join(record["root_path"], record["plan_path"])
        if not os.path.exists(plan_path):
            from core.projectspec import render_template
            with open(plan_path, "w", encoding="utf-8") as fh:
                fh.write(render_template(name=record["name"]))
    return record


def auth_snapshot(cfg):
    from core.authx import backend_auth_report
    memory = str(config_mod.AGENTIC_DIR / "memory")
    return {"backends": backend_auth_report(cfg, memory)}


def mcp_snapshot(cfg):
    from core.mcp import MCPGateway
    gateway = MCPGateway(cfg, _registry().home)
    return {"servers": gateway.list()}


def mcp_action(cfg, server_id, action):
    from core.mcp import MCPGateway
    gateway = MCPGateway(cfg, _registry().home)
    actions = {"enable": gateway.enable, "disable": gateway.disable,
               "test": gateway.test, "review": gateway.mark_reviewed,
               "authenticate": gateway.authenticate,
               "remove": gateway.remove}
    if action not in actions:
        raise ValueError("unsupported action %r" % action)
    return actions[action](server_id)


def market_snapshot(cfg):
    from core.skillmarket import SkillMarket
    market = SkillMarket(cfg, str(config_mod.AGENTIC_DIR),
                         _registry().home)
    catalog = market._load_catalog()
    updates = market.check_updates()
    return {"candidates": sorted(catalog.values(),
                                 key=lambda c: c.get("id", "")),
            "update_available": updates["update_available"]}


def market_action(cfg, skill_id, action):
    from core.skillmarket import SkillMarket
    market = SkillMarket(cfg, str(config_mod.AGENTIC_DIR),
                         _registry().home)
    actions = {"quarantine": market.quarantine,
               "evaluate": market.evaluate,
               "approve": market.approve, "reject": market.reject,
               "rollback": market.rollback}
    if action not in actions:
        raise ValueError("unsupported action %r" % action)
    return actions[action](skill_id)


def capability_snapshot(cfg, project_id):
    """Read-only capability-intelligence view for one project (Phase 12):
    Capability Plan summary, Capability Graph satisfaction breakdown, the
    pending setup-actions autonomy inbox, and the Completion Contract
    from the most recent final audit. A project that hasn't run
    `capability plan`/`project start` yet gets None/empty fields --
    never fabricated."""
    registry = _registry()
    record = registry.get(project_id)
    plan = projectops.load_capability_plan(registry, project_id)
    graph = projectops.load_capability_graph(registry, project_id)
    graph_summary = None
    if graph is not None:
        by_state = {}
        for node in graph.nodes_of_type("capability").values():
            state = node["attributes"]["state"]
            by_state[state] = by_state.get(state, 0) + 1
        graph_summary = {
            "capability_count": len(graph.nodes_of_type("capability")),
            "by_state": by_state,
            "unresolved": graph.unresolved_capabilities(),
        }
    setup_actions = projectops.list_project_setup_actions(
        registry, project_id, status="pending")
    proj_cfg = projectops.project_cfg_for(cfg, registry, record)
    runtime_dir = proj_cfg["runtime"]["project_dir"]
    audit = projstate.read_yaml(runtime_dir, "final-audit.yaml", None)
    plan_summary = None
    if plan is not None:
        plan_summary = {
            "confidence": plan.get("confidence"),
            "required_capabilities": [r["capability_id"] for r in
                                      plan.get("required_capabilities", [])],
            "optional_capabilities": [r["capability_id"] for r in
                                      plan.get("optional_capabilities", [])],
            "protected_actions": plan.get("protected_actions", []),
            "unresolved_questions": plan.get("unresolved_questions", []),
        }
    return {"project_id": project_id, "plan_summary": plan_summary,
           "graph_summary": graph_summary, "setup_actions": setup_actions,
           "completion_contract": (audit or {}).get("completion_contract")}


def orchestration_snapshot(cfg):
    """Read-only Model Capability Registry + frontier-capacity view
    (Phase 12) -- the persisted snapshot only (`models refresh`/`doctor`
    populate it); a dashboard read never triggers live model discovery."""
    from core.capacity import CapacityLedger
    from core.hierarchy import frontier_capacity_status
    from core.modelcap import load_registry
    memory_dir = str(config_mod.AGENTIC_DIR / "memory")
    registry = load_registry(memory_dir)
    capacity = None
    if registry is not None:
        ledger = CapacityLedger(cfg, memory_dir)
        status, detail = frontier_capacity_status(cfg, ledger, registry)
        capacity = dict(detail, status=status)
    return {"generated_at": registry.generated_at if registry else None,
           "records": registry.records if registry else [],
           "frontier_capacity": capacity}
