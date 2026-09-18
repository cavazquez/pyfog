#!/usr/bin/env python3
"""Create, verify and restore a self-contained SQLite/image-store backup.

The command intentionally supports the project's SQLite deployment only. PostgreSQL or another
database must use its native consistent-dump tooling and the same image-store verification step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyfog.config import Settings
from pyfog.image_manifest import ImageManifest, parse_image_manifest, verify_image_artifacts

BACKUP_FORMAT = "pyfog-backup"
BACKUP_VERSION = 1
CONFIG_REFERENCE = {
    "database": "Restaurar la base SQLite desde database.sqlite3 y apuntar PYFOG_DATABASE_URL.",
    "image_store": "Restaurar image-store y apuntar PYFOG_IMAGE_STORE al directorio.",
    "session_key": (
        "No se incluye. Recuperar PYFOG_SECRET_KEY o PYFOG_SECRET_KEY_FILE "
        "desde el gestor de secretos."
    ),
    "tls": (
        "No se incluyen certificados ni claves privadas. Recuperarlos desde el gestor de secretos."
    ),
    "agent_tokens": "Rotar o volver a emitir tokens de equipos después de una recuperación.",
}


class BackupError(RuntimeError):
    """The backup is incomplete, inconsistent or unsafe to restore."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or "?" in database_url:
        raise BackupError("El backup/restore sólo admite una URL SQLite sin parámetros.")
    value = database_url[len(prefix) :]
    if value in {":memory:", ""}:
        raise BackupError("La base SQLite debe ser un archivo persistente.")
    return Path(value).resolve() if not value.startswith("/") else Path(value)


def require_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.exists() or not path.is_dir():
        raise BackupError(f"{label} no es un directorio seguro: {path}")


def iter_files(root: Path) -> Iterator[Path]:
    """Yield regular files while rejecting symlinks anywhere in a backup tree."""

    require_directory(root, "El directorio")
    if not root.exists():
        return
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.is_symlink():
            raise BackupError(f"No se permiten enlaces simbólicos en el backup: {entry}")
        if entry.is_dir():
            yield from iter_files(entry)
        elif entry.is_file():
            yield entry
        else:
            raise BackupError(f"Entrada no regular en el backup: {entry}")


def copy_tree(source: Path, destination: Path, label: str) -> None:
    if not source.exists():
        if source.is_symlink():
            raise BackupError(f"{label} no es un directorio seguro: {source}")
        destination.mkdir(parents=True, exist_ok=True, mode=0o750)
        return
    require_directory(source, label)
    destination.mkdir(parents=True, exist_ok=True, mode=0o750)
    if not source.exists():
        return
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        target = destination / entry.name
        if entry.is_symlink():
            raise BackupError(f"No se permiten enlaces simbólicos en {label}: {entry}")
        if entry.is_dir():
            copy_tree(entry, target, label)
        elif entry.is_file():
            shutil.copy2(entry, target)
        else:
            raise BackupError(f"Entrada no regular en {label}: {entry}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise BackupError(f"No se pudo leer {path}: {error}") from None
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def sqlite_revision(database: Path) -> str | None:
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
    except sqlite3.OperationalError as error:
        if "no such table: alembic_version" in str(error):
            return None
        raise BackupError(f"No se pudo leer la versión de la base: {error}") from None
    except sqlite3.Error as error:
        raise BackupError(f"No se pudo leer la versión de la base: {error}") from None
    return str(row[0]) if row else None


def backup_database(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise BackupError(f"No existe una base SQLite regular para respaldar: {source}")
    try:
        with (
            sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_connection,
            sqlite3.connect(destination) as destination_connection,
        ):
            source_connection.backup(destination_connection)
        destination.chmod(0o600)
    except (OSError, sqlite3.Error) as error:
        raise BackupError(f"No se pudo crear la copia consistente de SQLite: {error}") from None


def file_records(root: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for path in iter_files(root):
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
            }
        )
    return records


def create_backup(database: Path, image_store: Path, output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise BackupError(f"El destino del backup ya existe: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial-{uuid.uuid4().hex}")
    try:
        partial.mkdir(mode=0o750)
        backup_database(database, partial / "database.sqlite3")
        copy_tree(image_store, partial / "image-store", "El almacén de imágenes")
        for name in ("staging", "published"):
            (partial / "image-store" / name).mkdir(mode=0o750, exist_ok=True)
        write_json(partial / "config-reference.json", CONFIG_REFERENCE)
        files = file_records(partial)
        write_json(
            partial / "backup-manifest.json",
            {
                "format": BACKUP_FORMAT,
                "version": BACKUP_VERSION,
                "created_at": utc_now(),
                "schema_revision": sqlite_revision(partial / "database.sqlite3"),
                "files": files,
            },
        )
        partial.rename(output)
    except (OSError, sqlite3.Error, BackupError):
        shutil.rmtree(partial, ignore_errors=True)
        raise


def load_backup_manifest(backup: Path) -> dict[str, Any]:
    path = backup / "backup-manifest.json"
    if path.is_symlink() or not path.is_file():
        raise BackupError("Falta backup-manifest.json.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError(f"El manifiesto de backup no es JSON válido: {error}") from None
    if (
        not isinstance(payload, dict)
        or payload.get("format") != BACKUP_FORMAT
        or payload.get("version") != BACKUP_VERSION
        or not isinstance(payload.get("files"), list)
    ):
        raise BackupError("El formato o versión del backup no es compatible.")
    return payload


def verify_file_records(backup: Path, manifest: dict[str, Any]) -> None:
    expected: set[str] = set()
    for item in manifest["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BackupError("El manifiesto de backup contiene un archivo inválido.")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise BackupError("El manifiesto de backup contiene una ruta insegura.")
        checksum = item.get("sha256")
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise BackupError("El manifiesto de backup contiene una suma inválida.")
        expected.add(relative.as_posix())
        path = backup / relative
        if path.is_symlink() or not path.is_file() or sha256_file(path) != checksum:
            raise BackupError(f"La suma del backup no coincide: {relative}")
    actual = {
        path.relative_to(backup).as_posix()
        for path in iter_files(backup)
        if path.name != "backup-manifest.json"
    }
    if actual != expected:
        raise BackupError("El backup contiene archivos faltantes o no declarados.")


def canonical_manifest_hash(manifest: ImageManifest) -> str:
    payload = json.dumps(
        manifest.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_catalog(backup: Path) -> None:
    database = backup / "database.sqlite3"
    image_store = backup / "image-store"
    published = image_store / "published"
    require_directory(image_store, "El almacén respaldado")
    require_directory(published, "La publicación respaldada")
    if not database.is_file() or database.is_symlink():
        raise BackupError("Falta database.sqlite3 en el backup.")
    try:
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise BackupError("La base del backup tiene referencias foráneas inválidas.")
            rows = connection.execute(
                "SELECT id, status, deleted_at, manifest_json, manifest_sha256 FROM images"
            ).fetchall()
    except sqlite3.Error as error:
        raise BackupError(f"No se pudo validar el catálogo del backup: {error}") from None
    catalog = {str(row["id"]): row for row in rows}
    for row in rows:
        image_id = str(row["id"])
        try:
            uuid.UUID(image_id)
        except ValueError:
            raise BackupError(f"La imagen tiene un UUID inválido: {image_id}") from None
        directory = published / image_id
        if row["deleted_at"] is not None:
            if directory.exists() or directory.is_symlink():
                raise BackupError(f"La imagen eliminada todavía tiene publicación: {image_id}")
            continue
        if row["status"] != "ready":
            if directory.exists() or directory.is_symlink():
                raise BackupError(f"Una imagen no lista tiene publicación: {image_id}")
            continue
        if not directory.is_dir() or directory.is_symlink():
            raise BackupError(f"Falta la publicación de la imagen lista: {image_id}")
        try:
            decoded = json.loads(row["manifest_json"])
            manifest = parse_image_manifest(decoded)
            if str(manifest.image_id) != image_id:
                raise BackupError(f"El manifiesto no coincide con la imagen: {image_id}")
            if row["manifest_sha256"] != canonical_manifest_hash(manifest):
                raise BackupError(f"La suma del manifiesto no coincide: {image_id}")
            verify_image_artifacts(manifest, directory)
        except (TypeError, json.JSONDecodeError, ValueError) as error:
            raise BackupError(
                f"La publicación de {image_id} no pasó la verificación: {error}"
            ) from None
    try:
        for directory in sorted(published.iterdir(), key=lambda item: item.name):
            if directory.is_symlink() or not directory.is_dir():
                raise BackupError(f"Entrada publicada insegura: {directory.name}")
            if directory.name not in catalog:
                raise BackupError(f"Publicación huérfana sin fila de catálogo: {directory.name}")
    except OSError as error:
        raise BackupError(f"No se pudo inspeccionar las publicaciones: {error}") from None


def verify_backup(backup: Path) -> dict[str, Any]:
    if backup.is_symlink() or not backup.is_dir():
        raise BackupError(f"El backup no es un directorio seguro: {backup}")
    manifest = load_backup_manifest(backup)
    verify_file_records(backup, manifest)
    verify_catalog(backup)
    return manifest


def require_empty_target(path: Path, label: str) -> None:
    if path.is_symlink():
        raise BackupError(f"El destino {label} es un enlace simbólico.")
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise BackupError(f"El destino {label} no está vacío: {path}")


def invalidate_after_restore(database: Path) -> None:
    timestamp = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    reason = "El servidor fue recuperado desde backup; requiere reconciliación del operador."
    try:
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM login_sessions")
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "agent_credentials" in tables:
                connection.execute(
                    "UPDATE agent_credentials SET revoked_at = ? WHERE revoked_at IS NULL",
                    (timestamp,),
                )
                connection.execute(
                    "UPDATE hosts SET token_hash = NULL, token_expires_at = NULL "
                    "WHERE token_hash IS NOT NULL"
                )
            connection.execute(
                """
                UPDATE task_attempts
                   SET finished_at = ?, phase = 'failed', failure_reason = ?
                 WHERE finished_at IS NULL
                   AND task_id IN (
                       SELECT id FROM tasks
                        WHERE status IN ('assigned', 'running', 'verifying')
                   )
                """,
                (timestamp, reason),
            )
            connection.execute(
                """
                UPDATE tasks
                   SET status = 'intervention_required', transfer_slot = NULL,
                       cancel_requested_at = NULL, cancel_acknowledged_at = NULL,
                       message = ?, failure_reason = ?, updated_at = ?
                 WHERE status IN ('assigned', 'running', 'verifying')
                   AND id IN (
                       SELECT task_id FROM task_attempts
                        WHERE finished_at IS NOT NULL AND failure_reason = ?
                   )
                """,
                (reason, reason, timestamp, reason),
            )
            connection.commit()
    except sqlite3.Error as error:
        raise BackupError(f"No se pudo invalidar sesiones y leases recuperadas: {error}") from None


def restore_backup(backup: Path, database: Path, image_store: Path) -> None:
    verify_backup(backup)
    if database.exists() and database.is_symlink():
        raise BackupError("La base de destino es un enlace simbólico.")
    if database.exists() and (not database.is_file() or database.stat().st_size > 0):
        raise BackupError("La base de destino debe pertenecer a una instalación vacía.")
    require_empty_target(image_store, "almacén de imágenes")
    database.parent.mkdir(parents=True, exist_ok=True)
    image_store.mkdir(parents=True, exist_ok=True, mode=0o750)
    shutil.copy2(backup / "database.sqlite3", database)
    database.chmod(0o600)
    copy_tree(backup / "image-store", image_store, "El almacén de imágenes respaldado")
    invalidate_after_restore(database)


def settings_defaults() -> tuple[Path, Path]:
    settings = Settings()
    return sqlite_path(settings.database_url), settings.image_store_path.resolve()


def parser() -> argparse.ArgumentParser:
    default_database, default_store = settings_defaults()
    root = argparse.ArgumentParser(description="Backup consistente de PyFog")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("backup", "verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--backup", type=Path, required=name != "backup")
        if name == "backup":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--database-url", default=f"sqlite:///{default_database}")
            command.add_argument("--image-store", type=Path, default=default_store)
        if name == "backup":
            command.add_argument("--database-url", default=f"sqlite:///{default_database}")
            command.add_argument("--image-store", type=Path, default=default_store)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "backup":
            create_backup(
                sqlite_path(args.database_url), args.image_store.resolve(), args.output.resolve()
            )
            sys.stdout.write(f"Backup creado en {args.output.resolve()}\n")
        elif args.command == "verify":
            manifest = verify_backup(args.backup.resolve())
            sys.stdout.write(
                f"Backup válido: revisión {manifest.get('schema_revision') or 'desconocida'}\n"
            )
        else:
            restore_backup(
                args.backup.resolve(),
                sqlite_path(args.database_url),
                args.image_store.resolve(),
            )
            sys.stdout.write("Backup restaurado; sesiones y leases activas requieren revisión.\n")
    except BackupError as error:
        sys.stderr.write(f"Error: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
