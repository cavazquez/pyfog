"""Create a reproducible, checksum-addressed source artifact for a PyFog release."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.release_check import ReleaseCheckError, check_source

ROOT = Path(__file__).resolve().parents[1]


def git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ReleaseCheckError("No se encontró git para crear el artefacto de fuente.")
    return executable


def git_output(*arguments: str) -> str:
    result = subprocess.run(  # noqa: S603 - arguments are fixed by this script
        [git_executable(), "-C", str(ROOT), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def ensure_clean_source() -> None:
    for arguments in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
        result = subprocess.run(  # noqa: S603 - arguments are fixed by this script
            [git_executable(), "-C", str(ROOT), *arguments], check=False
        )
        if result.returncode != 0:
            raise ReleaseCheckError("El checkout tiene cambios; empaquetá un commit limpio.")


def package(output: Path, expected_version: str | None) -> Path:
    version = check_source(expected_version)
    ensure_clean_source()
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise ReleaseCheckError("El directorio de salida no es seguro.")
    if output.resolve() in {ROOT.resolve(), Path("/")}:
        raise ReleaseCheckError("El directorio de salida es demasiado amplio.")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ReleaseCheckError(f"El directorio de salida no está vacío: {output}.")

    commit = git_output("rev-parse", "HEAD")
    archive_name = f"pyfog-{version}-source.tar"
    archive = output / archive_name
    archive_data = subprocess.run(  # noqa: S603 - git arguments are fixed below
        [
            git_executable(),
            "-C",
            str(ROOT),
            "archive",
            "--format=tar",
            f"--prefix=pyfog-{version}/",
            "HEAD",
        ],
        check=True,
        capture_output=True,
    ).stdout
    archive.write_bytes(archive_data)
    digest = hashlib.sha256(archive_data).hexdigest()
    manifest = {
        "schema_version": 1,
        "format": "pyfog-source-release",
        "version": version,
        "commit": commit,
        "archive": {"path": archive_name, "sha256": digest, "size": len(archive_data)},
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sums = "\n".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in (archive, manifest_path)
    )
    (output / "SHA256SUMS").write_text(sums + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/release")
    parser.add_argument("--version", help="Versión esperada; por defecto, la del proyecto.")
    args = parser.parse_args()
    try:
        output = package(args.output_dir, args.version)
    except (OSError, ReleaseCheckError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f"package-release: error: {error}\n")
        return 1
    sys.stdout.write(f"Fuente PyFog empaquetada en {output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
