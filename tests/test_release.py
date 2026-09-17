import hashlib
import json
from pathlib import Path

import pytest

from scripts.release_check import (
    ReleaseCheckError,
    check_agent,
    check_pxe,
    check_source,
    verify_checksum_bundle,
)


def write_checksums(directory: Path) -> None:
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(directory).as_posix()}"
        for path in files
    ]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_source_versions_and_release_documents_match() -> None:
    assert check_source("0.1.0") == "0.1.0"


def test_release_bundles_require_valid_manifests_and_checksums(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    for name, content in (("vmlinuz", b"kernel"), ("initramfs.img", b"initramfs")):
        (agent / name).write_bytes(content)
    (agent / "manifest.json").write_text(
        json.dumps(
            {"schema_version": 1, "format": "pyfog-agent-initramfs", "agent_version": "0.1.0"}
        ),
        encoding="utf-8",
    )
    write_checksums(agent)
    check_agent(agent, "0.1.0")

    pxe = tmp_path / "pxe"
    (pxe / "http/agent").mkdir(parents=True)
    (pxe / "tftp").mkdir()
    (pxe / "http/agent/vmlinuz").write_bytes(b"kernel")
    (pxe / "tftp/boot.ipxe").write_text("#!ipxe\n", encoding="utf-8")
    (pxe / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "format": "pyfog-pxe-profile",
                "profile_version": "0.1.0",
                "base_url": "https://pyfog.example/boot",
            }
        ),
        encoding="utf-8",
    )
    write_checksums(pxe)
    check_pxe(pxe, "0.1.0")

    (pxe / "tftp/boot.ipxe").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ReleaseCheckError, match="Checksum inválido"):
        verify_checksum_bundle(pxe)
