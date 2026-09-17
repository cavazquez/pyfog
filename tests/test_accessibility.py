from pathlib import Path

from pyfog.config import PACKAGE_DIR

TEMPLATE_DIR = PACKAGE_DIR / "templates"


def read_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def test_primary_flows_have_labels_errors_and_preserved_values():
    flows = {
        "login.html": ('for="username"', 'for="password"', 'autocomplete="username"'),
        "host_form.html": ('for="name"', 'for="mac_address"', 'aria-invalid="true"'),
        "import.html": ('for="inventory"', 'aria-describedby="file-help"', 'role="alert"'),
        "capture.html": ('for="disk_key"', 'for="image_id"', 'aria-invalid="true"'),
        "deployment.html": ('for="image_id"', 'for="disk_key"', "required"),
    }
    for template, markers in flows.items():
        content = read_template(template)
        assert all(marker in content for marker in markers), template
    assert "values.get('hostname', '')" in read_template("deployment.html")
    assert "values.get('image_name', '')" in read_template("capture.html")


def test_tables_and_progress_have_non_visual_descriptions():
    for template in (
        "hosts.html",
        "images.html",
        "tasks.html",
        "host_detail.html",
        "inventory.html",
    ):
        content = read_template(template)
        assert "<caption" in content, template
    task_detail = read_template("task_detail.html")
    assert 'role="status"' in task_detail
    assert 'aria-label="Progreso de la tarea"' in task_detail
    assert 'id="task-bytes"' in task_detail


def test_css_and_javascript_cover_keyboard_mobile_and_busy_states():
    css = (PACKAGE_DIR / "static/app.css").read_text(encoding="utf-8")
    javascript = (PACKAGE_DIR / "static/app.js").read_text(encoding="utf-8")
    assert ":focus-visible" in css
    assert "max-width: 760px" in css
    assert "max-width: 480px" in css
    assert ".table-wrap" in css
    assert "aria-busy" in javascript
    assert "button.disabled = true" in javascript


def test_visual_evidence_matrix_is_kept_with_the_project():
    evidence = Path(__file__).parents[1] / "docs/accessibility.md"
    content = evidence.read_text(encoding="utf-8")
    for viewport in ("360", "768", "1280"):
        assert viewport in content
    assert "Tab" in content
    assert "captura" in content.lower()
