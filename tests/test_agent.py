import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"


def test_agent_entrypoints_are_executable_and_shell_valid() -> None:
    entrypoints = [
        AGENT / "build-agent",
        AGENT / "init",
        AGENT / "pyfog-agent",
        AGENT / "udhcpc.script",
    ]
    for entrypoint in entrypoints:
        assert entrypoint.stat().st_mode & stat.S_IXUSR
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 - fixed repository-local scripts
        [bash, "-n", *(str(path) for path in entrypoints)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_agent_help_describes_safe_default() -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository-local command
        [str(AGENT / "build-agent"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "inventory-only" in result.stdout
    assert "Partclone" in result.stdout


def test_inventory_only_doctor_accepts_a_readable_kernel() -> None:
    required = [
        "bash",
        "cpio",
        "gzip",
        "sha256sum",
        "find",
        "sort",
        "ldd",
        "readlink",
        "python3",
        "busybox",
        "curl",
        "openssl",
        "ip",
    ]
    if any(shutil.which(command) is None for command in required):
        pytest.skip("faltan herramientas opcionales del entorno de construcción")
    result = subprocess.run(  # noqa: S603 - fixed repository-local command
        [
            str(AGENT / "build-agent"),
            "doctor",
            "--mode",
            "inventory-only",
            "--kernel",
            "/bin/true",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Requisitos completos." in result.stdout
