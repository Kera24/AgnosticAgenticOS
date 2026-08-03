"""Platform-owned local static application smoke evidence."""
from core import gate


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_local_static_app_smoke_serves_mount_and_local_asset_graph(tmp_path):
    _write(tmp_path / "index.html", """
        <main id="app"></main>
        <link rel="stylesheet" href="src/app.css">
        <script type="module" src="src/index.js"></script>
    """)
    _write(tmp_path / "src" / "app.css", "body { display: block; }")
    _write(tmp_path / "src" / "index.js", """
        const template = new URL('./components/task-form.html', import.meta.url);
        fetch(template);
    """)
    _write(tmp_path / "src" / "components" / "task-form.html",
           "<form><input name='title'></form>")

    result = gate.run_local_static_app_smoke(str(tmp_path))

    assert result["passed"] is True
    assert result["exit_code"] == 0
    assert result["platform_neutral"] is True
    assert "index.html" in result["detail"]
    assert "src/index.js" in result["detail"]
    assert "src/components/task-form.html" in result["detail"]


def test_local_static_app_smoke_accepts_non_empty_semantic_main(tmp_path):
    _write(tmp_path / "index.html", """
        <main><h1>Tip Calculator</h1></main>
        <script src="app.js"></script>
    """)
    _write(tmp_path / "app.js", "console.log('ok')")

    result = gate.run_local_static_app_smoke(str(tmp_path))

    assert result["passed"] is True
    assert "application root present" in result["detail"]


def test_local_static_app_smoke_fails_when_application_root_is_missing(tmp_path):
    _write(tmp_path / "index.html", "<script src='app.js'></script>")
    _write(tmp_path / "app.js", "console.log('ok')")

    result = gate.run_local_static_app_smoke(str(tmp_path))

    assert result["passed"] is False
    assert "neither #app mount nor non-empty <main> root" in result["detail"]


def test_local_static_app_smoke_rejects_empty_semantic_main(tmp_path):
    _write(tmp_path / "index.html", "<main><!-- placeholder --></main>")

    result = gate.run_local_static_app_smoke(str(tmp_path))

    assert result["passed"] is False
    assert "neither #app mount nor non-empty <main> root" in result["detail"]


def test_local_static_app_smoke_fails_when_local_asset_is_missing(tmp_path):
    _write(tmp_path / "index.html",
           "<div id='app'></div><script src='missing.js'></script>")

    result = gate.run_local_static_app_smoke(str(tmp_path))

    assert result["passed"] is False
    assert "local static app load failed" in result["detail"]


def test_local_static_app_smoke_ignores_external_asset_urls(tmp_path):
    _write(tmp_path / "index.html", """
        <div id="app"></div>
        <script src="https://example.invalid/external.js"></script>
        <script src="app.js"></script>
    """)
    _write(tmp_path / "app.js", "console.log('local')")

    result = gate.run_local_static_app_smoke(str(tmp_path))

    assert result["passed"] is True
    assert "external.js" not in result["detail"]
