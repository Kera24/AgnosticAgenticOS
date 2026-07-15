"""Standing goals: deterministic predicates that must keep passing after a
task completes. Goals live in .agentic/goals/*.yaml:

  id: no-todo-in-core
  description: core modules stay TODO-free
  predicate: "python -c \"import sys; sys.exit(0)\""   # exit 0 == satisfied
  born: 2026-07-15
  status: active          # active | retired  (retiring requires a human edit)
  last_pass: null
  on_violation: alert
  retire_when: "human decision only"
"""
import datetime as _dt
import os
import subprocess

import yaml

LEDGER_COLUMNS = ["timestamp", "goal_id", "result", "detail"]


def load_goals(goals_dir):
    goals = []
    if not os.path.isdir(goals_dir):
        return goals
    for name in sorted(os.listdir(goals_dir)):
        if not name.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(goals_dir, name), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        data["_file"] = name
        goals.append(data)
    return goals


def _run_predicate(predicate, cwd, timeout):
    try:
        proc = subprocess.run(predicate, shell=True, cwd=cwd,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout + proc.stderr)[-300:].strip()
    except subprocess.TimeoutExpired:
        return False, "predicate timed out after %ss" % timeout


def check_goals(cfg, agentic_dir, repo_root_path):
    """Run every active goal predicate. Returns (violations, results).
    Appends to the goal ledger."""
    goals_dir = os.path.join(agentic_dir, "goals")
    ledger = os.path.join(agentic_dir, "memory", "goal-ledger.tsv")
    timeout = int(cfg.get("execution", {}).get("goal_timeout_seconds", 60))
    results, violations = [], []
    for goal in load_goals(goals_dir):
        if goal.get("status") != "active" or not goal.get("predicate"):
            continue
        ok, detail = _run_predicate(goal["predicate"], repo_root_path, timeout)
        results.append({"id": goal.get("id", goal["_file"]), "ok": ok,
                        "detail": detail,
                        "on_violation": goal.get("on_violation", "alert")})
        if not ok:
            violations.append(results[-1])
        _append_ledger(ledger, goal.get("id", goal["_file"]),
                       "pass" if ok else "violation", detail)
        if ok:
            _touch_last_pass(os.path.join(goals_dir, goal["_file"]), goal)
    return violations, results


def _append_ledger(path, goal_id, result, detail):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        if new:
            fh.write("\t".join(LEDGER_COLUMNS) + "\n")
        fh.write("%s\t%s\t%s\t%s\n" % (
            _dt.datetime.now().isoformat(timespec="seconds"), goal_id, result,
            detail.replace("\t", " ").replace("\n", " ")))


def _touch_last_pass(path, goal):
    goal = {k: v for k, v in goal.items() if not k.startswith("_")}
    goal["last_pass"] = _dt.datetime.now().isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(goal, fh, sort_keys=False)


def propose_goal(agentic_dir, goal_id, description, predicate):
    """Create a new standing goal file (deterministic predicate required)."""
    if not predicate or not str(predicate).strip():
        raise ValueError("a standing goal requires a deterministic predicate")
    path = os.path.join(agentic_dir, "goals", goal_id + ".yaml")
    goal = {"id": goal_id, "description": description, "predicate": predicate,
            "born": _dt.date.today().isoformat(), "status": "active",
            "last_pass": None, "on_violation": "alert",
            "retire_when": "human decision only"}
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(goal, fh, sort_keys=False)
    return path
