import pytest
from fastapi import HTTPException

from pyfog.api import artifact_route_path


def test_artifact_route_accepts_bios_boot_sector():
    assert artifact_route_path("boot-sector.bin") == "boot-sector.bin"


def test_artifact_route_rejects_unscoped_paths():
    with pytest.raises(HTTPException):
        artifact_route_path("metadata.json")
