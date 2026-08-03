"""Fix for the canonical-contract divergence: the architect backlog,
compiled work order, bootstrap validator, worker prompt, and recovery
engine each had their own view of "what a task's expected outputs are" --
the conductor's work order could declare outputs (`.gitignore`,
`src/index.js`, a `tests` directory) the backlog's own `expected_paths`
never knew about, and the worker's own "blocked" self-report (a
fabricated capability claim) was copied verbatim into a blocking_reason
with no code/failure_class at all.

This uses the EXACT shapes from the live evidence (run 20260726-020710):
backlog `expected_paths` = [package.json, index.html, {src, directory}];
work-order `expected_outputs` = [package.json, index.html, .gitignore,
src/index.js, tests]; `allowed_paths` additionally covers `.gitignore`,
`src/**/*`, `tests/**/*`."""
import json
import os

from conftest import Clock, FakeCaller, git, project_cfg, seed_project, simple_task, worker_out
from core import (cachestore, contract as contract_mod, contract_recovery,
                  fscap, gate, gitops, preflight as preflight_mod, projstate)
from core.project import run_cycle

LIVE_BACKLOG_EXPECTED_PATHS = [
    {"path": "package.json", "type": "file", "required": True,
     "non_empty": True},
    {"path": "index.html", "type": "file", "required": True,
     "non_empty": True},
    {"path": "src", "type": "directory", "required": True},
]

LIVE_WORK_ORDER_EXPECTED_OUTPUTS = [
    "package.json", "index.html", ".gitignore", "src/index.js", "tests/",
]

LIVE_ALLOWED_PATHS = ["package.json", "index.html", ".gitignore",
                      "src/**/*", "tests/**/*"]


def _live_order(**over):
    order = {"action": "execute", "item": "scaffold the static app",
             "skill": "scaffold", "spec": "scaffold",
             "done_when": [{"id": "DW-1", "condition": "scaffolded",
                            "command": None}],
             "allowed_paths": LIVE_ALLOWED_PATHS, "forbidden_paths": [],
             "maximum_changed_lines": 200, "risk": "low",
             "queue_reason": None,
             "expected_outputs": LIVE_WORK_ORDER_EXPECTED_OUTPUTS}
    order.update(over)
    return order


def _live_task(**over):
    task = simple_task("t1-init-repo", kind="bootstrap",
                       expected_paths=LIVE_BACKLOG_EXPECTED_PATHS,
                       acceptance_criteria=["scaffolded"])
    task.update(over)
    return task


# -- item 3: "src/**/*"-shaped descendant patterns cover a direct child --------

def test_double_star_slash_star_pattern_covers_direct_child_file():
    """The live evidence's own allowed_paths used `src/**/*` (not the
    more common `src/**`) -- a real, separate glob-matching defect where
    a trailing "/**/*'" required a SECOND path separator, so it silently
    rejected a direct child like `src/index.js` and even the bare `src`
    directory itself."""
    assert gitops.match_pattern("src/index.js", "src/**/*")
    assert gitops.match_pattern("src/a/b.js", "src/**/*")   # still works
    assert gitops.directory_is_allowed("src", ["src/**/*"])
    assert gitops.directory_is_allowed("tests", ["tests/**/*"])


# -- 1/2: backlog/work-order divergence rejected before backend invocation -----

def test_live_divergence_detected_by_compiled_contract():
    task = _live_task()
    order = _live_order()
    contract = contract_mod.build_task_contract(task, order, "ollama-pilot")
    divergences = contract_mod.find_work_order_divergences(contract)
    assert divergences   # .gitignore/src/index.js/tests not yet declared
    assert any(".gitignore" in d for d in divergences)
    assert any("src/index.js" in d for d in divergences)
    assert any("tests" in d for d in divergences)


def test_live_divergence_fails_preflight_as_platform_invalid(tmp_path):
    task = _live_task()
    order = _live_order()
    contract = contract_mod.build_task_contract(task, order, "ollama-pilot")
    result = preflight_mod.run_preflight(contract, task, [task],
                                         str(tmp_path), str(tmp_path))
    assert result["result"] == preflight_mod.RESULT_PLATFORM_INVALID
    names = {c["name"]: c for c in result["checks"]}
    assert names["work_order_matches_compiled_contract"]["ok"] is False


def test_live_divergence_is_projected_onto_canonical_contract():
    task = _live_task()
    proposed = _live_order()
    stable = contract_mod.canonicalize_work_order(task, proposed)
    compiled = contract_mod.build_task_contract(
        task, stable, "ollama-pilot")

    canonical_paths = {
        entry["path"] for entry in compiled["required_outputs"]}
    assert canonical_paths == {"package.json", "index.html", "src"}
    assert stable["expected_outputs"] == [
        "package.json", "index.html", "src"]
    assert ".gitignore" not in stable["expected_outputs"]
    assert "src/index.js" not in stable["expected_outputs"]
    assert "tests/" not in stable["expected_outputs"]
    assert contract_mod.find_work_order_divergences(compiled) == []


# -- migration: item 1's six typed required outputs, item 8's exact fix -------

def test_migration_compiles_contract_with_all_six_typed_outputs(sandbox):
    project_cfg(sandbox)
    task = _live_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    added = contract_recovery.migrate_task_contract(
        a, str(sandbox["agentic"] / "memory"), sandbox["cfg"],
        "t1-init-repo", contract_recovery.OLLAMA_PILOT_T1_INIT_REPO_ENTRIES)
    assert {e["path"] for e in added} == {".gitignore", "src/index.js",
                                          "tests"}
    migrated = {t["id"]: t for t in projstate.load_backlog(a)}["t1-init-repo"]
    paths = {e["path"] if isinstance(e, dict) else e
            for e in migrated["expected_paths"]}
    assert paths == {"package.json", "index.html", "src", ".gitignore",
                     "src/index.js", "tests"}
    assert len(migrated["expected_paths"]) == 6

    order = _live_order()
    contract = contract_mod.build_task_contract(migrated, order,
                                                "ollama-pilot")
    assert contract_mod.find_work_order_divergences(contract) == []


def test_gitignore_directory_and_descendant_and_tests_all_accepted(sandbox):
    """.gitignore accepted; src (directory) + src/index.js (descendant
    file) both accepted; tests directory accepted -- once migrated, none
    of the live work order's expected_outputs diverge."""
    project_cfg(sandbox)
    task = _live_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    contract_recovery.migrate_task_contract(
        a, str(sandbox["agentic"] / "memory"), sandbox["cfg"],
        "t1-init-repo", contract_recovery.OLLAMA_PILOT_T1_INIT_REPO_ENTRIES)
    migrated = {t["id"]: t for t in projstate.load_backlog(a)}["t1-init-repo"]
    contract = contract_mod.build_task_contract(migrated, _live_order(),
                                                "ollama-pilot")
    required = {e["path"]: e["type"] for e in contract["required_outputs"]}
    assert required[".gitignore"] == "file"
    assert required["src"] == "directory"
    assert required["src/index.js"] == "file"
    assert required["tests"] == "directory"
    assert contract_mod.find_work_order_divergences(contract) == []


def test_full_cycle_succeeds_once_migrated_and_worker_produces_all_outputs(
        sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = _live_task()
    test_setup = simple_task("t2-tests", kind="test_setup",
                             dependencies=["t1-init-repo"])
    seed_project(sandbox, [task, test_setup])
    a = str(sandbox["agentic"])
    contract_recovery.migrate_task_contract(
        a, str(sandbox["agentic"] / "memory"), sandbox["cfg"],
        "t1-init-repo", contract_recovery.OLLAMA_PILOT_T1_INIT_REPO_ENTRIES)
    caller = FakeCaller({
        "conductor": _live_order(),
        "coder": worker_out(edits=[
            {"path": "package.json", "action": "write", "content": "{}\n"},
            {"path": "index.html", "action": "write",
             "content": "<html></html>"},
            {"path": ".gitignore", "action": "write",
             "content": "node_modules/\n"},
            {"path": "src", "action": "mkdir"},
            {"path": "src/index.js", "action": "write",
             "content": "console.log('hi');\n"},
            {"path": "tests", "action": "mkdir"}]),
        "qa": {"verdict": "pass", "done_when_results": [], "reason": "ok",
              "out_of_scope_changes": [], "test_integrity_preserved": True},
        "security": {"verdict": "pass", "concerns": [], "reason": "clean"}})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success", result
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-init-repo"]["status"] == "done"


# -- item 5: contradicted model-declared capability claims ---------------------

def test_fscap_recognises_mkdir_and_git_init_claims_as_contradicted():
    assert fscap.contradicted_capability_claim(
        "cannot create the src directory") == "create_directory"
    assert fscap.contradicted_capability_claim(
        "WORKER role cannot create the src directory") is not None
    assert fscap.contradicted_capability_claim(
        "src/ and tests/ directories require mkdir/git init which is "
        "unavailable") is not None
    assert fscap.contradicted_capability_claim(
        "the deterministic test suite failed on line 42") is None


def test_contradicted_claim_permits_one_corrective_retry(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    from conftest import proj_order
    caller = FakeCaller({
        "conductor": proj_order(task),
        "coder": [
            worker_out(blocked=True,
                      blocker="cannot create directory; mkdir is not "
                              "available to this role"),
            worker_out(edits=[{"path": "src/app.py", "action": "write",
                              "content": "VALUE = 2\n"}])],
        "qa": {"verdict": "pass", "done_when_results": [], "reason": "ok",
              "out_of_scope_changes": [], "test_integrity_preserved": True},
        "security": {"verdict": "pass", "concerns": [], "reason": "clean"}})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success", result
    coder_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(coder_calls) == 2   # one corrective retry, never zero/more


def test_contradicted_claim_never_persisted_verbatim_after_repair_exhausted(
        sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["repair"] = {"maximum_attempts_per_task": 1,
                               "maximum_replans_per_task": 2}
    task = simple_task()
    seed_project(sandbox, [task])
    from conftest import proj_order
    claim = "WORKER role cannot create the tests directory whatsoever"
    caller = FakeCaller({
        "conductor": proj_order(task),
        "coder": [worker_out(blocked=True, blocker=claim)] * 3})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "failure"
    a = str(sandbox["agentic"])
    blockers = projstate.open_blockers(a)
    assert blockers
    assert claim not in blockers[0]["reason"]   # never copied verbatim
    assert blockers[0]["failure_class"] == "model_output_invalid"
    assert blockers[0]["code"] == \
        projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH


# -- item 6: historical context hygiene + cache invalidation -------------------

def test_resolved_historical_blocker_memory_excluded_from_active_search(
        sandbox):
    project_cfg(sandbox)
    task = _live_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    memory_dir = str(sandbox["agentic"] / "memory")
    from core import memsvc
    service = memsvc.get_memory(sandbox["cfg"], memory_dir)
    record_id = service.save(
        "failed_attempt", "task t1-init-repo blocked",
        "bootstrap-expected-paths validation rejected .gitignore",
        task_id="t1-init-repo")
    live_reason = ("all outputs consistently rejected by "
                  "bootstrap-expected-paths validation; src/ and tests/ "
                  "directories require mkdir/git init")
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=live_reason)
    projstate.add_blocker(a, "t1-init-repo", live_reason, human_only=False,
                          code=None, memory_record_id=record_id)

    before = service.search("bootstrap-expected-paths", task_id="t1-init-repo")
    assert any(r["id"] == record_id for r in before)

    contract_recovery.recover_contract_divergence_blockers(a, memory_dir,
                                                           sandbox["cfg"])
    after = service.search("bootstrap-expected-paths", task_id="t1-init-repo")
    assert not any(r["id"] == record_id for r in after)   # superseded


def test_cache_invalidates_only_affected_task_after_contract_change(sandbox):
    project_cfg(sandbox)
    task = _live_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    memory_dir = str(sandbox["agentic"] / "memory")
    store = cachestore.CacheStore(memory_dir)
    key_affected = store.put_artifact(
        "k1", "stale rendered prefix", "prompt_prefix_text",
        dependencies={"task_contract_hash:t1-init-repo": "old-hash"})["key"]
    key_other_task = store.put_artifact(
        "k2", "unrelated content", "prompt_prefix_text",
        dependencies={"task_contract_hash:t2-other": "old-hash"})["key"]
    live_reason = ("all outputs consistently rejected by "
                  "bootstrap-expected-paths validation; src/ and tests/ "
                  "directories require mkdir/git init")
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=live_reason)
    projstate.add_blocker(a, "t1-init-repo", live_reason, human_only=False,
                          code=None)

    contract_recovery.recover_contract_divergence_blockers(a, memory_dir,
                                                           sandbox["cfg"])
    assert store.get(key_affected)[1] is False   # invalidated
    assert store.get(key_other_task)[1] is True   # untouched -- different task


# -- item 7: cross-platform deterministic checks -------------------------------

def test_posix_existence_idiom_translated_to_platform_neutral_check(
        tmp_path):
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    cfg = {"verification": {"commands": [
        {"name": "pkg-exists", "command": "test -f package.json",
         "mandatory": True},
        {"name": "src-exists", "command": "test -d src", "mandatory": True},
    ]}}
    result = gate.run_checks(cfg, str(tmp_path))
    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["pkg-exists"]["passed"] is True
    assert by_name["pkg-exists"]["platform_neutral"] is True
    assert by_name["src-exists"]["passed"] is False   # src/ doesn't exist yet
    assert by_name["src-exists"]["platform_neutral"] is True


def test_unix_only_command_never_silently_executed_or_passed_on_windows(
        tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    cfg = {"verification": {"commands": [
        {"name": "listing", "command": "ls -la src", "mandatory": True}]}}
    result = gate.run_checks(cfg, str(tmp_path))
    record = result["results"][0]
    assert record["passed"] is False
    assert record.get("skipped_unix_only") is True
    assert result["ok"] is False   # never silently converts to a pass


def test_rm_rf_never_a_permitted_command_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    from core import execpolicy
    result = execpolicy.run_allowlisted(
        "rm -rf /", ["rm -rf /"], str(tmp_path), timeout=5)
    assert result is None   # refused even though it's on the allowlist


# -- idempotence / preserving genuine project state ----------------------------

def test_migration_recovery_is_idempotent(sandbox):
    project_cfg(sandbox)
    task = _live_task()
    other = simple_task("t2-untouched", status="done", last_result="pass")
    seed_project(sandbox, [task, other])
    a = str(sandbox["agentic"])
    memory_dir = str(sandbox["agentic"] / "memory")
    live_reason = ("all outputs consistently rejected by "
                  "bootstrap-expected-paths validation; src/ and tests/ "
                  "directories require mkdir/git init")
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=live_reason, attempts=5)
    projstate.add_blocker(a, "t1-init-repo", live_reason, human_only=False,
                          code=None)

    first = contract_recovery.recover_contract_divergence_blockers(
        a, memory_dir, sandbox["cfg"])
    assert len(first) == 1 and first[0]["task_id"] == "t1-init-repo"
    second = contract_recovery.recover_contract_divergence_blockers(
        a, memory_dir, sandbox["cfg"])
    assert second == []   # nothing left to migrate

    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-init-repo"]["status"] == "pending"
    assert tasks["t1-init-repo"]["status"] != "done"
    assert tasks["t1-init-repo"]["attempts"] == 0
    # genuine project history untouched
    assert tasks["t2-untouched"]["status"] == "done"
    assert tasks["t2-untouched"]["last_result"] == "pass"


def test_unrelated_blocker_never_touched_by_contract_recovery(sandbox):
    project_cfg(sandbox)
    task = simple_task("t1-first")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    reason = "conductor queued: awaiting architecture clarification"
    projstate.update_task(a, "t1-first", status="blocked",
                          blocking_reason=reason)
    projstate.add_blocker(a, "t1-first", reason, human_only=False, code=None)
    events = contract_recovery.recover_contract_divergence_blockers(
        a, str(sandbox["agentic"] / "memory"), sandbox["cfg"])
    assert events == []
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-first"]["status"] == "blocked"


def test_work_order_prose_outputs_match_typed_glob_and_json_member():
    task = simple_task(
        "t9-test-suite-setup",
        expected_paths=[
            {"path": "tests/*.test.js", "type": "glob", "required": True},
            {"path": "package.json#scripts.test", "type": "file",
             "required": True},
        ])
    order = _live_order(expected_outputs=[
        "Updated `package.json` with `scripts.test`.",
        "One or more JS tests in `tests/*.test.js` covering CRUD.",
    ])
    compiled = contract_mod.build_task_contract(
        task, order, "ollama-pilot", "20260728-055526")

    assert contract_mod.find_work_order_divergences(compiled) == []

    compiled["work_order_expected_outputs"].append(
        "Also create `unplanned.config.js`.")
    assert any("unplanned.config.js" in problem for problem in
               contract_mod.find_work_order_divergences(compiled))


def test_artifact_backed_recovery_clears_only_now_valid_structural_blocker(
        sandbox):
    project_cfg(sandbox)
    task = simple_task(
        "t9-test-suite-setup",
        expected_paths=[
            {"path": "tests/*.test.js", "type": "glob", "required": True},
            {"path": "package.json#scripts.test", "type": "file",
             "required": True},
        ])
    seed_project(sandbox, [task])
    agentic_dir = str(sandbox["agentic"])
    reason = "preflight platform_invalid: false prose path comparison"
    projstate.update_task(agentic_dir, task["id"], status="blocked",
                          blocking_reason=reason, last_result="failure")
    projstate.add_blocker(
        agentic_dir, task["id"], reason,
        code=projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH,
        human_only=False)

    order = _live_order(expected_outputs=[
        "Updated `package.json` with `scripts.test`.",
        "One or more JS tests in `tests/*.test.js` covering CRUD.",
    ])
    compiled = contract_mod.build_task_contract(
        task, order, "ollama-pilot", "r9")
    run_dir = sandbox["agentic"] / "runs" / "cycle-r9"
    run_dir.mkdir(parents=True)
    (run_dir / "task-contract.json").write_text(
        json.dumps(compiled), encoding="utf-8")

    events = contract_recovery.recover_fixed_contract_comparison_blockers(
        agentic_dir, str(sandbox["agentic"] / "memory"), sandbox["cfg"])

    current = {t["id"]: t for t in projstate.load_backlog(agentic_dir)}
    assert current[task["id"]]["status"] == "pending"
    assert current[task["id"]]["blocking_reason"] is None
    assert projstate.open_blockers(agentic_dir) == []
    assert events[0]["action"] == \
        "reset_false_structural_contract_mismatch"
    assert events[0]["run_id"] == "r9"


def test_stable_projection_discards_conductor_scope_expansion():
    task = simple_task(
        "t5-task-list-renderer",
        expected_paths=[
            {"path": "src/components/task-list.html", "type": "file",
             "required": True},
            {"path": "tests/t5-renderer.test.js", "type": "file",
             "required": True},
        ],
        acceptance_criteria=["renderer works"],
        deterministic_checks=[
            "node -e \"require('./dist/tests/t5-ui')\""])
    proposed = _live_order(
        expected_outputs=[
            "src/components/task-list.html",
            "tests/t5-renderer.test.js",
            "dist/tests/t5-ui",
        ],
        allowed_paths=["src/index.js", "dist/tests/t5-ui"])
    proposed["acceptance_criteria"] = ["conductor invented criterion"]
    proposed["deterministic_checks"] = ["conductor invented command"]

    stable = contract_mod.canonicalize_work_order(task, proposed)
    compiled = contract_mod.build_task_contract(
        task, stable, "ollama-pilot", "r-stable")

    assert stable["expected_outputs"] == [
        "src/components/task-list.html",
        "tests/t5-renderer.test.js",
    ]
    assert stable["acceptance_criteria"] == ["renderer works"]
    assert stable["deterministic_checks"] == [
        "node -e \"require('./dist/tests/t5-ui')\""]
    assert "src/components/task-list.html" in stable["allowed_paths"]
    assert "tests/t5-renderer.test.js" in stable["allowed_paths"]
    assert contract_mod.find_work_order_divergences(compiled) == []


def test_stable_projection_maps_json_member_to_writable_document():
    task = simple_task(
        "t9-test-suite-setup",
        expected_paths=[{
            "path": "package.json#scripts.test",
            "type": "file",
            "required": True,
        }])
    stable = contract_mod.canonicalize_work_order(
        task, _live_order(allowed_paths=[]))

    assert stable["expected_outputs"] == ["package.json#scripts.test"]
    assert stable["allowed_paths"] == ["package.json"]


def test_stable_authority_recovery_resets_conductor_expansion_blocker(
        sandbox):
    project_cfg(sandbox)
    task = simple_task(
        "t5-task-list-renderer",
        expected_paths=[{
            "path": "tests/t5-renderer.test.js",
            "type": "file",
            "required": True,
        }])
    seed_project(sandbox, [task])
    agentic_dir = str(sandbox["agentic"])
    reason = ("preflight platform_invalid: work-order expected output "
              "'dist/tests/t5-ui' is not present in the compiled task "
              "contract's required_outputs")
    projstate.update_task(agentic_dir, task["id"], status="blocked",
                          blocking_reason=reason, last_result="failure")
    projstate.add_blocker(
        agentic_dir, task["id"], reason,
        code=projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH,
        human_only=False)

    proposed = _live_order(
        expected_outputs=[
            "tests/t5-renderer.test.js", "dist/tests/t5-ui"],
        allowed_paths=["tests/t5-renderer.test.js", "dist/tests/t5-ui"])
    compiled = contract_mod.build_task_contract(
        task, proposed, "ollama-pilot", "r-expand")
    run_dir = sandbox["agentic"] / "runs" / "cycle-r-expand"
    run_dir.mkdir(parents=True)
    (run_dir / "task-contract.json").write_text(
        json.dumps(compiled), encoding="utf-8")

    events = contract_recovery.recover_stable_contract_authority_blockers(
        agentic_dir, str(sandbox["agentic"] / "memory"), sandbox["cfg"])

    current = {item["id"]: item for item in
               projstate.load_backlog(agentic_dir)}
    assert current[task["id"]]["status"] == "pending"
    assert current[task["id"]]["blocking_reason"] is None
    assert projstate.open_blockers(agentic_dir) == []
    assert events[0]["action"] == "reset_conductor_contract_expansion"
    assert events[0]["discarded_conductor_outputs"] == [
        "tests/t5-renderer.test.js", "dist/tests/t5-ui"]
