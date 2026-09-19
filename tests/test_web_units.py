from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from pyfog.image_manifest import ImageManifest
from pyfog.models import Image
from pyfog.web import (
    current_section,
    format_bytes,
    format_date,
    image_status_label,
    inventory_disks,
    set_flash,
    take_flash,
    task_disk_selector,
    task_disk_selectors,
)
from tests.test_image_manifest import valid_manifest_v2


def request_with_session(path: str = "/images") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "session": {},
        }
    )


def test_web_formatters_navigation_flash_and_status_labels():
    assert format_bytes(None) == "No disponible"
    assert format_bytes(1024) == "1.0 KiB"
    assert format_bytes(1024**5) == "1.0 PiB"
    assert format_date(None) == "Todavía sin inventario"
    assert format_date(datetime(2026, 9, 19, 12, 30, tzinfo=UTC)) == "19/09/2026 · 12:30 UTC"
    assert current_section("/images/abc") == "images"
    assert current_section("/unknown") == "hosts"

    request = request_with_session()
    set_flash(request, "Guardado")
    assert take_flash(request) == {"level": "success", "message": "Guardado"}
    assert take_flash(request) is None
    request.session["flash"] = {"level": "error", "message": "ignored"}
    assert take_flash(request) is None

    image = Image(name="Image", status="ready")
    assert image_status_label(image) == "Lista"
    image.deleted_at = datetime.now(UTC)
    assert image_status_label(image) == "Eliminada"


def test_inventory_disks_assign_stable_identity_or_fail_closed(inventory):
    inventory["disks"] = [
        {"name": "/dev/sda", "size_bytes": 10, "wwn": "wwn-1"},
        {"name": "/dev/sdb", "size_bytes": 20, "serial_number": "serial-1"},
        {"name": "/dev/sdc", "size_bytes": 30},
    ]
    report = SimpleNamespace(data=inventory)
    disks = inventory_disks(report)
    assert [disk["stable_id"] for disk in disks] == [
        "wwn:wwn-1",
        "serial:serial-1",
        "path:/dev/sdc",
    ]
    assert inventory_disks(SimpleNamespace(data={"invalid": True})) == []
    assert inventory_disks(None) == []


def test_task_disk_selectors_preserve_v1_shape_and_manifest_requirements():
    disk = {
        "stable_id": "wwn:disk-a",
        "name": "/dev/sda",
        "size_bytes": 2 * 1024**3,
        "logical_sector_bytes": 512,
        "removable": False,
    }
    single = task_disk_selector(disk, operation="capture", consistency="hot")
    assert single["stable_id"] == "wwn:disk-a"
    assert single["consistency"] == "hot"

    manifest = ImageManifest.model_validate(valid_manifest_v2())
    clone = task_disk_selector(
        disk,
        operation="clone",
        manifest=manifest,
        clone_hostname="target.example",
    )
    assert clone["operation"] == "clone"
    assert clone["clone_hostname"] == "target.example"
    assert clone["source_size_bytes"] == manifest.disk.size_bytes

    assert task_disk_selectors([disk], consistency="hot") == single
    multiple = task_disk_selectors([disk, {**disk, "stable_id": "wwn:disk-b"}])
    assert len(multiple["disks"]) == 2
    with pytest.raises(ValueError, match="al menos un disco"):
        task_disk_selectors([])
    with pytest.raises(ValueError, match="cantidad"):
        task_disk_selectors([disk, disk], manifest=manifest)
