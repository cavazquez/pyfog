import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from pyfog.config import Settings
from pyfog.models import Host, LoginSession, now
from pyfog.security import digest
from tests.conftest import csrf, issue_token


def test_anonymous_users_cannot_view_or_mutate_inventory(client, host_id):
    for path in ("/hosts", "/hosts/new", f"/hosts/{host_id}", f"/hosts/{host_id}/import"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    response = client.post("/hosts/new", data={"name": "forbidden"}, follow_redirects=False)
    assert response.status_code == 303


def test_login_and_mutations_require_csrf(client, admin, host_id):
    assert client.post("/login", data={"username": "admin", "password": "test"}).status_code == 403
    assert admin.post("/hosts/new", data={"name": "invalid"}).status_code == 403
    assert admin.post(f"/hosts/{host_id}/token", data={"action": "generate"}).status_code == 403
    assert admin.get("/logout").status_code == 405


def test_logout_invalidates_copied_session_cookie(admin):
    cookie = admin.cookies.get("pyfog_session")
    admin.post("/logout", data={"csrf": csrf(admin)})
    response = admin.get(
        "/hosts", headers={"Cookie": f"pyfog_session={cookie}"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_server_side_session_expiry(admin, app):
    with Session(app.state.engine) as db:
        session = db.scalar(select(LoginSession))
        session.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert admin.get("/hosts", follow_redirects=False).status_code == 303


def test_login_is_rate_limited(client):
    token = csrf(client, "/login")
    for _ in range(8):
        response = client.post(
            "/login", data={"csrf": token, "username": "missing", "password": "wrong"}
        )
        assert response.status_code == 401
        assert "Usuario o contraseña incorrectos" in response.text
    assert (
        client.post(
            "/login", data={"csrf": token, "username": "admin", "password": "test-admin-password"}
        ).status_code
        == 429
    )


def test_inventory_token_is_hashed_scoped_rotatable_and_revocable(admin, app, host_id, inventory):
    first = issue_token(admin, host_id)
    path = f"/api/v1/hosts/{host_id}/inventory"
    with Session(app.state.engine) as db:
        host = db.get(Host, host_id)
        assert host.token_hash == digest(first)
        assert first not in host.token_hash
        other = Host(name="Other", mac_address="52:54:00:00:11:22")
        db.add(other)
        db.commit()
        other_id = other.id
    assert first not in admin.get(f"/hosts/{host_id}").text
    headers = {"Authorization": f"Bearer {first}"}
    assert admin.post(path, json=inventory).status_code == 401
    assert (
        admin.post(
            f"/api/v1/hosts/{other_id}/inventory", json=inventory, headers=headers
        ).status_code
        == 401
    )
    second = issue_token(admin, host_id)
    assert first != second
    assert admin.post(path, json=inventory, headers=headers).status_code == 401
    headers = {"Authorization": f"Bearer {second}"}
    assert admin.post(path, json=inventory, headers=headers).status_code == 201
    admin.post(f"/hosts/{host_id}/token", data={"csrf": csrf(admin), "action": "revoke"})
    assert admin.post(path, json=inventory, headers=headers).status_code == 401


def test_expired_token_and_missing_equipment_have_same_auth_response(
    admin, app, host_id, inventory
):
    token = issue_token(admin, host_id)
    with Session(app.state.engine) as db:
        db.get(Host, host_id).token_expires_at = now() - timedelta(seconds=1)
        db.commit()
    headers = {"Authorization": f"Bearer {token}"}
    expired = admin.post(f"/api/v1/hosts/{host_id}/inventory", json=inventory, headers=headers)
    missing = admin.post(f"/api/v1/hosts/{uuid.uuid4()}/inventory", json=inventory, headers=headers)
    assert expired.status_code == missing.status_code == 401
    assert expired.json() == missing.json()


def test_oversized_body_is_rejected_even_without_content_length(client, host_id):
    response = client.post(
        f"/api/v1/hosts/{host_id}/inventory",
        content=iter([b"x" * 600_000, b"y" * 600_000]),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_untrusted_host_and_xss_are_blocked(admin, app, host_id):
    assert admin.get("/hosts", headers={"host": "untrusted.example"}).status_code == 400
    with Session(app.state.engine) as db:
        db.get(Host, host_id).notes = '<script>alert("xss")</script>'
        db.commit()
    response = admin.get(f"/hosts/{host_id}")
    assert response.status_code == 200
    assert '<script>alert("xss")</script>' not in response.text
    assert "&lt;script&gt;" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_production_requires_a_secret_and_secure_cookies():
    import pytest

    from pyfog.app import create_app

    with pytest.raises(ValueError, match="PYFOG_SECRET_KEY"):
        Settings(production=True, secret_key="")
    settings = Settings(production=True, secret_key="explicit-secret-with-at-least-32-characters")
    app = create_app(settings)
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="https://localhost") as client:
        response = client.get("/login")
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "secure" in cookie
        assert "samesite=lax" in cookie
    app.state.engine.dispose()
