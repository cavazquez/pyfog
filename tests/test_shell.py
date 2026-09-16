import re

from pyfog.config import PACKAGE_DIR
from tests.conftest import csrf


def test_workspace_navigation_has_one_active_section_and_local_assets(admin):
    pages = {
        "/hosts": "Equipos",
        "/images": "Imágenes",
        "/tasks": "Tareas",
    }
    for path, label in pages.items():
        response = admin.get(path)
        assert response.status_code == 200
        assert f'href="{path}" class="nav-item active" aria-current="page"' in response.text
        assert response.text.count('aria-current="page"') == 1
        assert label in response.text
        assert "/static/app.css" in response.text
        assert "/static/app.js" in response.text
        resource_urls = re.findall(r'(?:src|<link[^>]+href)="([^"]+)"', response.text)
        assert all(url.endswith(("/static/app.css", "/static/app.js")) for url in resource_urls)


def test_planned_sections_explain_their_current_state(admin):
    images = admin.get("/images")
    tasks = admin.get("/tasks")
    assert "Las imágenes llegan después del arranque por red." in images.text
    assert "Las tareas aparecerán cuando exista un agente para ejecutarlas." in tasks.text
    assert "Volver a equipos" in images.text


def test_success_notice_is_announced_once_after_registering_a_host(admin):
    response = admin.post(
        "/hosts/new",
        data={
            "csrf": csrf(admin),
            "name": "Equipo con aviso",
            "mac_address": "52:54:00:12:aa:bb",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    first_page = admin.get(location)
    assert 'role="status"' in first_page.text
    assert "Equipo registrado. Ya podés cargar su primer inventario." in first_page.text
    assert (
        "Equipo registrado. Ya podés cargar su primer inventario." not in admin.get(location).text
    )


def test_post_forms_expose_busy_feedback_and_duplicate_submit_protection(admin):
    form_page = admin.get("/hosts/new")
    script = (PACKAGE_DIR / "static" / "app.js").read_text()
    assert 'data-loading-text="Guardando…"' in form_page.text
    assert 'id="submit-status"' in form_page.text
    assert 'aria-live="polite"' in form_page.text
    assert 'form.dataset.submitting === "true"' in script
    assert "button.disabled = true" in script
    assert 'form.setAttribute("aria-busy", "true")' in script
