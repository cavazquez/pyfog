import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pyfog.app import create_app
from pyfog.config import Settings
from pyfog.models import Host, User
from pyfog.security import passwords


@pytest.fixture(scope="session")
def password_hash():
    return passwords.hash("test-admin-password")


@pytest.fixture
def app(tmp_path, password_hash):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        allowed_hosts=["testserver", "localhost", "127.0.0.1"],
        secret_key="test-only-session-secret-at-least-32-characters",
    )
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")
    application = create_app(settings)
    with Session(application.state.engine) as db:
        db.add(User(username="admin", password_hash=password_hash))
        db.commit()
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as connection:
        yield connection


def csrf(client, path="/hosts"):
    response = client.get(path)
    assert response.status_code == 200, response.text
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match[1]


@pytest.fixture
def admin(client):
    response = client.post(
        "/login",
        data={
            "csrf": csrf(client, "/login"),
            "username": "admin",
            "password": "test-admin-password",
        },
    )
    assert response.status_code == 200
    return client


@pytest.fixture
def host_id(app):
    with Session(app.state.engine) as db:
        host = Host(name="Linux del laboratorio", mac_address="52:54:00:12:34:56")
        db.add(host)
        db.commit()
        return host.id


@pytest.fixture
def inventory():
    return {
        "schema_version": 1,
        "report_id": str(uuid.uuid4()),
        "collected_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "hostname": "linux-lab-01",
        "os": {"name": "Ubuntu 24.04 LTS", "id": "ubuntu", "version": "24.04"},
        "kernel": "6.8.0-test",
        "architecture": "x86_64",
        "cpu": {"model": "CPU de prueba", "logical_cores": 8},
        "memory": {"total_bytes": 16 * 1024**3},
        "system": {"manufacturer": "Laboratorio", "model": "VM", "serial_number": "DEMO-001"},
        "disks": [{"name": "/dev/vda", "size_bytes": 128 * 1024**3}],
        "interfaces": [{"name": "enp1s0", "mac_address": "52:54:00:12:34:56", "state": "up"}],
        "warnings": [],
    }


def issue_token(admin, host_id):
    response = admin.post(
        f"/hosts/{host_id}/token",
        data={"csrf": csrf(admin), "action": "generate"},
    )
    assert response.status_code == 200
    match = re.search(r'<textarea id="token"[^>]*>([^<]+)</textarea>', response.text)
    assert match
    return match[1]
