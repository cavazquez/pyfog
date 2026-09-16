import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyfog.models import Host, InventoryReport
from pyfog.schemas import Inventory, normalize_mac
from tests.conftest import csrf, issue_token


@pytest.mark.parametrize("mac", ["52:54:AB:12:34:56", "52-54-AB-12-34-56", "5254ab123456"])
def test_mac_formats_are_canonical(mac):
    assert normalize_mac(mac) == "52:54:ab:12:34:56"


@pytest.mark.parametrize(
    "mac", ["", "ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00", "01:54:00:12:34:56", "52:54-00:12:34:56"]
)
def test_invalid_mac_is_rejected(mac):
    with pytest.raises(ValueError, match="MAC"):
        normalize_mac(mac)


def test_create_edit_and_duplicate_host(admin, app):
    values = {"name": "Equipo uno", "mac_address": "52-54-AB-12-34-56", "notes": "Aula 1"}
    response = admin.post(
        "/hosts/new", data={"csrf": csrf(admin), **values}, follow_redirects=False
    )
    assert response.status_code == 303
    location = response.headers["location"]
    duplicate = admin.post("/hosts/new", data={"csrf": csrf(admin), **values})
    assert duplicate.status_code == 422
    assert "Ya existe" in duplicate.text
    edited = admin.post(
        location + "/edit",
        data={
            "csrf": csrf(admin),
            "name": "Renombrado",
            "notes": "Aula 2",
            "mac_address": "ignored",
        },
    )
    assert edited.status_code == 200
    with Session(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(Host)) == 1
        host = db.scalar(select(Host))
        assert (host.name, host.mac_address, host.notes) == (
            "Renombrado",
            "52:54:ab:12:34:56",
            "Aula 2",
        )


def test_import_is_idempotent_and_history_is_immutable(admin, app, host_id, inventory):
    payload = json.dumps(inventory)
    for _ in range(2):
        response = admin.post(
            f"/hosts/{host_id}/import",
            data={"csrf": csrf(admin)},
            files={"inventory": ("report.json", payload, "application/json")},
        )
        assert response.status_code == 200
        assert "16.0 GiB" in response.text
        assert "CPU de prueba" in response.text
    with Session(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(InventoryReport)) == 1
    inventory["cpu"]["model"] = "Contenido alterado"
    response = admin.post(
        f"/hosts/{host_id}/import",
        data={"csrf": csrf(admin)},
        files={"inventory": ("report.json", json.dumps(inventory))},
    )
    assert response.status_code == 422
    assert "otro contenido" in response.text
    assert "CPU de prueba" in admin.get(f"/hosts/{host_id}").text


def test_api_reports_are_idempotent_and_old_reports_do_not_replace_current(
    admin, app, host_id, inventory
):
    token = issue_token(admin, host_id)
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/hosts/{host_id}/inventory"
    assert admin.post(path, json=inventory, headers=headers).status_code == 201
    assert admin.post(path, json=inventory, headers=headers).status_code == 200
    older = copy.deepcopy(inventory)
    older["report_id"] = str(uuid.uuid4())
    older["hostname"] = "old-hostname"
    older["collected_at"] = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    assert admin.post(path, json=older, headers=headers).status_code == 201
    with Session(app.state.engine) as db:
        assert db.get(Host, host_id).hostname == inventory["hostname"]
        assert db.scalar(select(func.count()).select_from(InventoryReport)) == 2
    page = admin.get(f"/hosts/{host_id}")
    assert "old-hostname" not in page.text
    inventory["hostname"] = "altered"
    assert admin.post(path, json=inventory, headers=headers).status_code == 409


def test_import_wrong_machine_does_not_change_host(admin, app, host_id, inventory):
    inventory["interfaces"][0]["mac_address"] = "52:54:00:99:99:99"
    result = admin.post(
        f"/hosts/{host_id}/import",
        data={"csrf": csrf(admin)},
        files={"inventory": ("report.json", json.dumps(inventory))},
    )
    assert result.status_code == 422
    assert "MAC principal" in result.text
    with Session(app.state.engine) as db:
        assert db.get(Host, host_id).last_inventory_at is None
        assert db.scalar(select(func.count()).select_from(InventoryReport)) == 0


@pytest.mark.parametrize("payload", ["not json", "{}", "[]", '{"schema_version": 2}'])
def test_bad_documents_leave_database_unchanged(admin, app, host_id, payload):
    response = admin.post(
        f"/hosts/{host_id}/import",
        data={"csrf": csrf(admin)},
        files={"inventory": ("bad.json", payload)},
    )
    assert response.status_code == 422
    with Session(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(InventoryReport)) == 0


@pytest.mark.parametrize("value", [True, "1", 1.0, 2])
def test_schema_version_is_explicit(inventory, value):
    inventory["schema_version"] = value
    with pytest.raises(ValueError, match="schema_version"):
        Inventory.model_validate(inventory)


def test_future_date_and_invalid_hardware_are_rejected(inventory):
    inventory["collected_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="futuro"):
        Inventory.model_validate(inventory)
    inventory["collected_at"] = datetime.now(UTC).isoformat()
    inventory["memory"]["total_bytes"] = -1
    with pytest.raises(ValueError, match="total_bytes"):
        Inventory.model_validate(inventory)


def test_catalog_search_filters_pagination_and_empty_state(admin, app, host_id):
    assert "Linux del laboratorio" in admin.get("/hosts?q=laboratorio").text
    assert "Linux del laboratorio" not in admin.get("/hosts?q=missing").text
    assert "No encontramos equipos" in admin.get("/hosts?state=ready").text
    assert "Linux del laboratorio" in admin.get("/hosts?state=pending").text
    with Session(app.state.engine) as db:
        for number in range(22):
            db.add(Host(name=f"Equipo {number:02d}", mac_address=f"52:54:00:aa:00:{number:02x}"))
        db.commit()
    assert "Página 1 de 2" in admin.get("/hosts").text
    assert "Página 2 de 2" in admin.get("/hosts?page=2").text
    assert "Página 2 de 2" in admin.get("/hosts?page=9999").text


def test_history_does_not_allow_cross_host_report_access(admin, app, host_id, inventory):
    token = issue_token(admin, host_id)
    admin.post(
        f"/api/v1/hosts/{host_id}/inventory",
        json=inventory,
        headers={"Authorization": f"Bearer {token}"},
    )
    with Session(app.state.engine) as db:
        report_id = db.scalar(select(InventoryReport.id))
        other = Host(name="Otro", mac_address="52:54:00:11:22:33")
        db.add(other)
        db.commit()
        other_id = other.id
    assert admin.get(f"/hosts/{host_id}/reports/{report_id}").status_code == 200
    assert admin.get(f"/hosts/{other_id}/reports/{report_id}").status_code == 404
