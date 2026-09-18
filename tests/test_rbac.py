from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyfog.database import make_engine
from pyfog.models import AuditEvent, User
from pyfog.rbac import ROLE_PERMISSIONS, has_permission
from tests.conftest import csrf


def test_permission_matrix_is_explicit_and_fails_closed():
    assert has_permission("admin", "images.delete")
    assert has_permission("operator", "images.capture")
    assert not has_permission("operator", "images.delete")
    assert not has_permission("auditor", "restore.execute")
    assert has_permission("auditor", "audit.read")
    assert has_permission("admin", "backup.restore")
    assert not has_permission("operator", "backup.restore")
    assert not has_permission("auditor", "unknown.permission")
    assert not has_permission("unknown-role", "hosts.read")
    assert set(ROLE_PERMISSIONS) == {"admin", "operator", "auditor"}


def test_mvp_users_are_migrated_to_admin(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "4e8c2b7a9d10")  # pragma: allowlist secret
    engine = make_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (username, password_hash) VALUES ('legacy', 'not-used')"
        )
    command.upgrade(config, "head")
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.username == "legacy"))
        assert user is not None
        assert user.role == "admin"
    engine.dispose()


def test_admin_can_assign_roles_and_cannot_remove_last_admin(admin, app, password_hash):
    with Session(app.state.engine) as db:
        db.add(User(username="operator", password_hash=password_hash, role="operator"))
        db.commit()
        operator = db.scalar(select(User).where(User.username == "operator"))
        admin_user = db.scalar(select(User).where(User.username == "admin"))
        assert operator is not None
        assert admin_user is not None
        operator_id, admin_id = operator.id, admin_user.id

    page = admin.get("/users")
    assert page.status_code == 200
    assert "operator" in page.text
    changed = admin.post(
        f"/users/{operator_id}/role",
        data={"csrf": csrf(admin, "/users"), "role": "auditor"},
        follow_redirects=False,
    )
    assert changed.status_code == 303
    with Session(app.state.engine) as db:
        assert db.get(User, operator_id).role == "auditor"

    last_admin = admin.post(
        f"/users/{admin_id}/role",
        data={"csrf": csrf(admin, "/users"), "role": "operator"},
    )
    assert last_admin.status_code == 409
    with Session(app.state.engine) as db:
        assert db.get(User, admin_id).role == "admin"


def test_operator_and_auditor_have_read_only_boundaries(client, app, password_hash, host_id):
    with Session(app.state.engine) as db:
        db.add_all(
            [
                User(username="operator", password_hash=password_hash, role="operator"),
                User(username="auditor", password_hash=password_hash, role="auditor"),
            ]
        )
        db.commit()

    assert (
        client.post(
            "/login",
            data={
                "csrf": csrf(client, "/login"),
                "username": "operator",
                "password": "test-admin-password",  # pragma: allowlist secret
            },
        ).status_code
        == 303
    )
    assert client.get("/hosts").status_code == 200
    assert client.get("/audit").status_code == 403
    assert client.get("/users").status_code == 403
    assert client.get("/images/new").status_code == 200

    client.post("/logout", data={"csrf": csrf(client)})
    assert (
        client.post(
            "/login",
            data={
                "csrf": csrf(client, "/login"),
                "username": "auditor",
                "password": "test-admin-password",  # pragma: allowlist secret
            },
        ).status_code
        == 303
    )
    hosts = client.get("/hosts")
    assert hosts.status_code == 200
    assert "Registrar equipo" not in hosts.text
    assert client.get("/images").status_code == 200
    assert (
        client.post(
            "/hosts/new",
            data={"csrf": csrf(client, "/hosts"), "name": "no autorizado"},
        ).status_code
        == 403
    )

    with Session(app.state.engine) as db:
        denied = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "access.denied")
            .order_by(AuditEvent.id.desc())
        ).first()
        assert denied is not None
        assert denied.decision == "deny"
        assert denied.resource_type == "route"
        assert denied.reason
        assert denied.actor_user_id is not None
        assert denied.reason == "Rol auditor sin permiso hosts.create."
