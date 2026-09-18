import hashlib
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PXE = ROOT / "pxe"


def test_pxe_builder_and_template_are_shell_valid() -> None:
    builder = PXE / "build-pxe"
    assert builder.stat().st_mode & stat.S_IXUSR
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 - fixed repository-local scripts
        [bash, "-n", str(builder)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_pxe_menu_has_local_default_and_no_chain_loop() -> None:
    template = (PXE / "boot.ipxe.tmpl").read_text(encoding="utf-8")
    assert "choose --default local --timeout" in template
    assert "goto local" in template
    assert "sanboot --no-describe --drive 0x80" in template
    assert "iseq ${platform} efi && sanboot --no-describe --drive 0" in template
    assert "pyfog.server=${pyfog_server} pyfog.pair=1" in template
    assert "chain " not in template


def test_pxe_builder_rejects_non_https_base_url() -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository-local command
        [
            str(PXE / "build-pxe"),
            "build",
            "--base-url",
            "http://pyfog.example/boot",
            "--profile-only",
            "--agent-dir",
            str(ROOT / "missing-agent"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "base-url" in result.stderr


def test_pxe_builder_publishes_a_verified_profile_without_ipxe_binary(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    output_dir = tmp_path / "pxe"
    agent_dir.mkdir()
    kernel = agent_dir / "vmlinuz"
    initramfs = agent_dir / "initramfs.img"
    kernel.write_bytes(b"kernel-fixture")
    initramfs.write_bytes(b"initramfs-fixture")
    manifest = {
        "schema_version": 1,
        "format": "pyfog-agent-initramfs",
        "agent_version": "fixture",
        "capabilities": ["inventory"],
        "artifacts": {
            "kernel": {
                "file": "vmlinuz",
                "sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
            },
            "initramfs": {
                "file": "initramfs.img",
                "sha256": hashlib.sha256(initramfs.read_bytes()).hexdigest(),
            },
        },
        "rootfs": {"files": []},
    }
    (agent_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (agent_dir / "SHA256SUMS").write_text(
        "\n".join(
            [
                f"{hashlib.sha256(kernel.read_bytes()).hexdigest()}  vmlinuz",
                f"{hashlib.sha256(initramfs.read_bytes()).hexdigest()}  initramfs.img",
                f"{hashlib.sha256((agent_dir / 'manifest.json').read_bytes()).hexdigest()}  "
                "manifest.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - fixed repository-local command
        [
            str(PXE / "build-pxe"),
            "build",
            "--agent-dir",
            str(agent_dir),
            "--output-dir",
            str(output_dir),
            "--base-url",
            "https://pyfog.example/boot",
            "--profile-only",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    profile = (output_dir / "tftp" / "boot.ipxe").read_text(encoding="utf-8")
    assert "https://pyfog.example/boot/agent/initramfs.img" in profile
    assert "pyfog.server=${pyfog_server} pyfog.pair=1" in profile
    assert "choose --default local --timeout 5000" in profile
    assert "token" not in profile.lower()
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["server_url"] == "https://pyfog.example"


def test_pxe_builder_signs_and_verifies_secure_boot_profile(tmp_path: Path) -> None:
    required = ("openssl", "osslsigncode", "sbverify")
    fixture = Path("/usr/lib/shim/shimx64.efi.signed.latest")
    if any(shutil.which(command) is None for command in required) or not fixture.is_file():
        pytest.skip("faltan herramientas o fixture EFI para Secure Boot")

    agent_dir = tmp_path / "agent"
    output_dir = tmp_path / "pxe"
    agent_dir.mkdir()
    kernel = agent_dir / "vmlinuz"
    initramfs = agent_dir / "initramfs.img"
    kernel.write_bytes(b"kernel-fixture")
    initramfs.write_bytes(b"initramfs-fixture")
    manifest = {
        "schema_version": 1,
        "format": "pyfog-agent-initramfs",
        "agent_version": "fixture",
        "capabilities": ["inventory"],
        "artifacts": {
            "kernel": {
                "file": "vmlinuz",
                "sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
            },
            "initramfs": {
                "file": "initramfs.img",
                "sha256": hashlib.sha256(initramfs.read_bytes()).hexdigest(),
            },
        },
        "rootfs": {"files": []},
    }
    (agent_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (agent_dir / "SHA256SUMS").write_text(
        "\n".join(
            [
                f"{hashlib.sha256(kernel.read_bytes()).hexdigest()}  vmlinuz",
                f"{hashlib.sha256(initramfs.read_bytes()).hexdigest()}  initramfs.img",
                f"{hashlib.sha256((agent_dir / 'manifest.json').read_bytes()).hexdigest()}  "
                "manifest.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    key = tmp_path / "release.key.pem"
    certificate = tmp_path / "release.cert.pem"
    openssl = shutil.which("openssl")
    assert openssl is not None
    subprocess.run(  # noqa: S603 - openssl is resolved from PATH above and paths are pytest-owned.
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-sha256",
            "-days",
            "1",
            "-subj",
            "/CN=PyFog test release",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    result = subprocess.run(  # noqa: S603 - fixed repository-local command and pytest-owned paths.
        [
            str(PXE / "build-pxe"),
            "build",
            "--agent-dir",
            str(agent_dir),
            "--output-dir",
            str(output_dir),
            "--base-url",
            "https://pyfog.example/boot",
            "--ipxe-efi",
            str(fixture),
            "--secure-boot",
            "--signing-key",
            str(key),
            "--signing-cert",
            str(certificate),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (output_dir / "secure-boot-manifest.json").is_file()
    verify = subprocess.run(  # noqa: S603 - fixed repository-local command and pytest-owned paths.
        [
            str(PXE / "build-pxe"),
            "verify",
            "--output-dir",
            str(output_dir),
            "--secure-boot-cert",
            str(certificate),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert verify.returncode == 0, verify.stderr


@pytest.mark.parametrize(
    "unsafe",
    [
        "https://user:pass@example.com/boot",  # pragma: allowlist secret
        "https://host/boot?x=1",
    ],
)
def test_pxe_builder_rejects_url_credentials_and_query(unsafe: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository-local command
        [str(PXE / "build-pxe"), "build", "--base-url", unsafe, "--profile-only"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "base-url" in result.stderr
