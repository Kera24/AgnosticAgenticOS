"""Phase 2 — code intelligence: adapters, fallback, exclusions, staleness,
token bounds, Windows paths, CCE mocking, and the retrieval benchmark."""
import json

from conftest import FakeRunner
from core.codeintel import ci_config, get_adapter, is_excluded
from core.codeintel.cce import CCEAdapter, CCEUnavailable
from core.codeintel.native import NativeAdapter
from core.codeintel.none_adapter import NoneAdapter


def ci_cfg(provider="native", fallback="none", **over):
    ci = {"provider": provider, "fallback": fallback,
          "index_on_project_start": True, "incremental_after_commit": True,
          "search_limit": 12, "expansion_limit": 4, "excluded_paths": []}
    ci.update(over)
    return {"context": {"code_intelligence": ci}}


def write(repo, rel, content):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# -- exclusions ---------------------------------------------------------------

def test_secret_and_dependency_paths_excluded():
    assert is_excluded(".env")
    assert is_excluded("config/.env.production")
    assert is_excluded("keys/server.pem")
    assert is_excluded("node_modules/lib/index.js")
    assert is_excluded(".git/config")
    assert is_excluded(".agentic/memory/usage.tsv")
    assert is_excluded("vendor/x.py", extra_excludes=["vendor/**"])
    assert not is_excluded("src/app.py")


def test_windows_backslash_paths_normalized():
    assert is_excluded("node_modules\\lib\\index.js")
    assert is_excluded(".agentic\\memory\\usage.tsv")


# -- none adapter -----------------------------------------------------------------

def test_none_adapter_graceful(tmp_path):
    adapter = NoneAdapter(str(tmp_path), str(tmp_path / "mem"))
    assert adapter.search("anything") == []
    assert adapter.health_check()["ok"]
    assert adapter.status()["provider"] == "none"
    assert adapter.index_full()["ok"]


# -- native adapter --------------------------------------------------------------

def test_native_index_and_search(sandbox):
    repo = sandbox["repo"]
    write(repo, "src/auth.py",
          "def authenticate(user, password):\n    return check(password)\n")
    write(repo, "src/other.py", "def unrelated():\n    pass\n")
    write(repo, ".env", "SECRET=x")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "more"], cwd=str(repo),
                   capture_output=True)
    adapter = NativeAdapter(str(repo), str(sandbox["agentic"] / "memory"),
                            cfg=ci_cfg()["context"]["code_intelligence"])
    result = adapter.index_full()
    assert result["ok"] and result["files_indexed"] >= 2
    hits = adapter.search("authenticate password")
    assert hits and hits[0]["path"] == "src/auth.py"
    assert all(h["path"] != ".env" for h in hits)
    status = adapter.status()
    assert status["indexed"] and not status["stale"]


def test_native_stale_detection(sandbox):
    repo = sandbox["repo"]
    memdir = str(sandbox["agentic"] / "memory")
    adapter = NativeAdapter(str(repo), memdir)
    adapter.index_full()
    assert adapter.status()["stale"] is False
    write(repo, "src/new.py", "x = 1\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "new"], cwd=str(repo),
                   capture_output=True)
    assert adapter.status()["stale"] is True
    adapter.index_changes(["src/new.py"], None)   # refreshes revision
    assert adapter.status()["stale"] is False


def test_native_token_budget_bounds_results(sandbox):
    repo = sandbox["repo"]
    for i in range(8):
        write(repo, "src/mod%d.py" % i,
              ("def target_function_%d():\n" % i) + "    data = 1\n" * 40)
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "mods"], cwd=str(repo),
                   capture_output=True)
    adapter = NativeAdapter(str(repo), str(sandbox["agentic"] / "memory"))
    unbounded = adapter.search("target_function data", limit=8)
    bounded = adapter.search("target_function data", limit=8,
                             token_budget=200)
    assert len(bounded) < len(unbounded)
    from core.context.tokenizer import estimate_tokens
    assert sum(estimate_tokens(r["snippet"]) for r in bounded) <= 200


def test_native_corrupt_index_state_recovers(sandbox):
    memdir = str(sandbox["agentic"] / "memory")
    adapter = NativeAdapter(str(sandbox["repo"]), memdir)
    adapter.index_full()
    with open(adapter._state_path(), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert adapter.load_index_state() is None
    assert adapter.status()["indexed"] is False   # honest, not crashed
    adapter.index_full()                          # reindex heals
    assert adapter.status()["indexed"] is True


# -- cce adapter (fully mocked; CCE never required) ----------------------------------

def cce_adapter(tmp_path, runner, which=lambda b: "C:/tools/cce.exe",
                cfg=None):
    return CCEAdapter(str(tmp_path), str(tmp_path / "mem"),
                      cfg=cfg or {}, runner=runner, which=which)


def test_cce_missing_binary_falls_back(tmp_path):
    cfg = ci_cfg(provider="cce", fallback="native")
    adapter = get_adapter(cfg, str(tmp_path), str(tmp_path / "mem"),
                          which=lambda b: None)
    assert adapter.provider_name == "native"
    assert "cce" in (adapter.fallback_reason or "")


def test_cce_unsupported_version_falls_back(tmp_path):
    cfg = ci_cfg(provider="cce", fallback="native")
    runner = FakeRunner([{"exit_code": 0, "stdout": "cce 9.0.0"}])
    adapter = get_adapter(cfg, str(tmp_path), str(tmp_path / "mem"),
                          runner=runner, which=lambda b: "cce")
    assert adapter.provider_name == "native"


def test_cce_search_sanitizes_results(tmp_path):
    results = {"results": [
        {"id": "r1", "path": "src/app.py", "start_line": 1, "end_line": 5,
         "snippet": "code", "score": 2.0, "language": "py"},
        {"id": "r2", "path": "../../outside.py", "start_line": 1,
         "end_line": 2, "snippet": "escape", "score": 9.9},
        {"id": "r3", "path": ".env", "start_line": 1, "end_line": 1,
         "snippet": "SECRET", "score": 9.9},
        "garbage",
        {"id": "r4", "path": "src/bad.py", "start_line": "NaN"},
    ]}
    runner = FakeRunner([
        {"exit_code": 0, "stdout": "cce 0.3.1"},        # detect
        {"exit_code": 0, "stdout": json.dumps(results)},
    ])
    adapter = cce_adapter(tmp_path, runner)
    hits = adapter.search("app")
    assert [h["id"] for h in hits] == ["r1"]
    # argv-only invocation, never a shell
    assert all(isinstance(c["argv"], list) for c in runner.calls)


def test_cce_malformed_output_and_timeout(tmp_path):
    runner = FakeRunner([
        {"exit_code": 0, "stdout": "cce 0.3.1"},
        {"exit_code": 0, "stdout": "NOT JSON"},
        {"exit_code": 0, "stdout": "cce 0.3.1"},
        {"exit_code": 0, "stdout": "", "timed_out": True},
    ])
    adapter = cce_adapter(tmp_path, runner)
    for expected in ("malformed", "timed out"):
        try:
            adapter.search("x")
            raise AssertionError("expected CCEUnavailable")
        except CCEUnavailable as exc:
            assert expected in str(exc)


def test_cce_index_changes_filters_secrets(tmp_path):
    runner = FakeRunner([
        {"exit_code": 0, "stdout": "cce 0.3.1"},
        {"exit_code": 0, "stdout": json.dumps({"files_indexed": 1})},
    ])
    adapter = cce_adapter(tmp_path, runner)
    adapter.index_changes(["src/app.py", ".env", "keys/k.pem"], "rev1")
    argv = runner.calls[-1]["argv"]
    # changed-file list sits between --changed and the first --exclude;
    # secret paths must be filtered out of it (patterns after --exclude
    # are exclusion VALUES and legitimately name .env etc.)
    changed = argv[argv.index("--changed") + 1:argv.index("--exclude")]
    assert changed == ["src/app.py"]


def test_cce_refuses_remote_endpoint(tmp_path):
    try:
        cce_adapter(tmp_path, FakeRunner([]),
                    cfg={"cce_endpoint": "https://cloud.example.com"})
        raise AssertionError("expected refusal")
    except CCEUnavailable as exc:
        assert "never sent" in str(exc)


# -- retrieval through the broker ------------------------------------------------------

def test_retrieval_items_feed_the_package(sandbox):
    from core.context.compose import retrieval_items
    repo = sandbox["repo"]
    write(repo, "src/value.py", "VALUE = 1\nDEFAULT_VALUE = 1\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "v"], cwd=str(repo),
                   capture_output=True)
    cfg = dict(sandbox["cfg"])
    cfg.update(ci_cfg(provider="native"))
    items = retrieval_items(cfg, "coder",
                            {"work_order": {"item": "change VALUE",
                                            "spec": "VALUE to 2"}},
                            str(repo), str(sandbox["agentic"] / "memory"))
    assert items
    assert all(i.trust_level == "untrusted" for i in items)
    assert all(i.category == "code" for i in items)
    assert any("value.py" in (i.source_path or "") for i in items)
    # non-retrieval roles and missing queries stay empty
    assert retrieval_items(cfg, "architect", {}, str(repo), "m") == []
    assert retrieval_items(cfg, "coder", {}, str(repo), "m") == []


# -- benchmark (LOCAL MEASUREMENT, repository fixtures only) ---------------------------

def test_benchmark_retrieval_vs_full_context(sandbox, capsys):
    """LOCAL MEASUREMENT on synthetic fixtures — not a general claim.
    Retrieved context must be materially smaller than full-file context
    while still containing the relevant code."""
    from core.context.tokenizer import estimate_tokens
    repo = sandbox["repo"]
    for i in range(12):
        write(repo, "src/module_%d.py" % i,
              "\n".join("def helper_%d_%d():\n    return %d" % (i, j, j)
                        for j in range(60)))
    write(repo, "src/target.py",
          "def compute_discount(price, rate):\n    return price * rate\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixtures"], cwd=str(repo),
                   capture_output=True)

    full_tokens = 0
    import os
    for base, _dirs, files in os.walk(str(repo / "src")):
        for name in files:
            with open(os.path.join(base, name), encoding="utf-8") as fh:
                full_tokens += estimate_tokens(fh.read())

    adapter = NativeAdapter(str(repo), str(sandbox["agentic"] / "memory"))
    hits = adapter.search("compute_discount price rate", limit=4)
    retrieved_tokens = sum(estimate_tokens(h["snippet"]) for h in hits)

    assert any(h["path"] == "src/target.py" for h in hits)
    assert retrieved_tokens < full_tokens * 0.2
    print("[local measurement] full=%d tokens retrieved=%d tokens "
          "(synthetic fixtures)" % (full_tokens, retrieved_tokens))
