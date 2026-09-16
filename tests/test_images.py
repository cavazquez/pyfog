import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pyfog.image_catalog import image_is_selectable
from pyfog.models import Image, now
from tests.conftest import csrf


def test_image_catalog_starts_empty_and_creates_a_stable_draft(admin, app):
    empty = admin.get("/images")
    assert empty.status_code == 200
    assert "Tu catálogo empieza acá" in empty.text
    assert "Crear primera imagen" in empty.text

    created = admin.post(
        "/images/new",
        data={
            "csrf": csrf(admin),
            "name": "Ubuntu aula 2026",
            "description": "Base del laboratorio",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    location = created.headers["location"]
    image_id = location.rsplit("/", maxsplit=1)[-1]

    detail = admin.get(location)
    assert detail.status_code == 200
    assert "Ubuntu aula 2026" in detail.text
    assert "Borrador" in detail.text
    assert "todavía no puede seleccionarse" in detail.text

    with Session(app.state.engine) as db:
        image = db.get(Image, image_id)
        assert image is not None
        assert image.name == "Ubuntu aula 2026"
        assert image.description == "Base del laboratorio"
        assert image.status == "draft"


def test_image_edit_keeps_identity_and_duplicate_names_are_rejected(admin, app):
    created = admin.post(
        "/images/new",
        data={"csrf": csrf(admin), "name": "Imagen original", "description": "v1"},
        follow_redirects=False,
    )
    location = created.headers["location"]
    image_id = location.rsplit("/", maxsplit=1)[-1]

    edited = admin.post(
        f"/images/{image_id}/edit",
        data={"csrf": csrf(admin), "name": "Imagen renombrada", "description": "v2"},
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert edited.headers["location"] == location

    duplicate = admin.post(
        "/images/new",
        data={"csrf": csrf(admin), "name": "Imagen renombrada", "description": "otra"},
    )
    assert duplicate.status_code == 422
    assert "Ya existe" in duplicate.text

    with Session(app.state.engine) as db:
        image = db.get(Image, image_id)
        assert image is not None
        assert image.name == "Imagen renombrada"
        assert image.description == "v2"


def test_image_input_rejects_blank_names(admin):
    response = admin.post(
        "/images/new", data={"csrf": csrf(admin), "name": "   ", "description": ""}
    )
    assert response.status_code == 422
    assert "at least 1 character" in response.text


def test_image_list_filters_status_and_paginates(admin, app, host_id):
    with Session(app.state.engine) as db:
        db.add(
            Image(
                name="Imagen lista",
                description="Publicada",
                status="ready",
                source_host_id=host_id,
                source_hostname="linux-lab-01",
                captured_at=now(),
                total_size_bytes=2 * 1024**3,
                compatibility="Ubuntu 24.04 · x86_64 · UEFI",
            )
        )
        db.add(Image(name="Imagen fallida", status="failed", failure_reason="Sin espacio"))
        db.add_all([Image(name=f"Borrador {index:02d}") for index in range(21)])
        db.commit()

    ready = admin.get("/images?status=ready")
    assert ready.status_code == 200
    assert "Imagen lista" in ready.text
    assert "Imagen fallida" not in ready.text
    assert "Ubuntu 24.04" in ready.text
    assert "2.0 GiB" in ready.text

    page_one = admin.get("/images?status=draft")
    assert "Página 1 de 2" in page_one.text
    assert "Borrador 00" in page_one.text
    page_two = admin.get("/images?status=draft&page=2")
    assert "Página 2 de 2" in page_two.text
    assert "Borrador 20" in page_two.text


def test_ready_selection_requires_manifest_and_integrity():
    ready = Image(
        name="Lista",
        status="ready",
        manifest_json={"format": "pyfog-disk-image"},
        integrity_verified_at=now(),
    )
    assert ready.integrity_verified_at is not None
    assert image_is_selectable(ready)

    ready.integrity_verified_at = None
    assert not image_is_selectable(ready)
    ready.integrity_verified_at = now()
    ready.manifest_json = None
    assert not image_is_selectable(ready)
    ready.status = "draft"
    ready.manifest_json = {"format": "pyfog-disk-image"}
    assert not image_is_selectable(ready)


def test_image_status_and_size_constraints_are_enforced(app):
    with Session(app.state.engine) as db:
        db.add(Image(name="Estado inválido", status="unknown"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(Image(name="Tamaño inválido", total_size_bytes=-1))
        with pytest.raises(IntegrityError):
            db.commit()
