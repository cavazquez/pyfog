"""Private, task-scoped storage for image artifacts."""

import contextlib
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pyfog.image_manifest import ImageManifest, validate_relative_path, verify_image_artifacts


class StorageError(ValueError):
    """A storage request cannot be completed safely."""


def _uuid_text(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise StorageError("El identificador de almacenamiento no es válido.") from None


@dataclass(frozen=True)
class ArtifactStore:
    """Store artifacts in server-created directories with an atomic publish boundary."""

    root: Path
    max_image_bytes: int = 2**50
    max_chunk_bytes: int = 512 * 1024
    min_free_bytes: int = 64 * 1024 * 1024

    def ensure_layout(self) -> Path:
        root = self.root
        if root.exists():
            if root.is_symlink() or not root.is_dir():
                raise StorageError("El almacén de imágenes no es un directorio seguro.")
        else:
            try:
                root.mkdir(parents=True, mode=0o750)
            except OSError as error:
                raise StorageError(f"No se pudo crear el almacén de imágenes: {error}") from None
        for name in ("staging", "published"):
            directory = root / name
            if directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    raise StorageError(f"La carpeta {name} del almacén no es segura.")
            else:
                try:
                    directory.mkdir(mode=0o750)
                except OSError as error:
                    raise StorageError(f"No se pudo preparar la carpeta {name}: {error}") from None
        return root

    def check_capacity(self, additional_bytes: int = 0) -> None:
        if additional_bytes < 0 or additional_bytes > self.max_image_bytes:
            raise StorageError("El tamaño de la imagen supera el límite configurado.")
        try:
            usage = shutil.disk_usage(self.ensure_layout())
        except OSError as error:
            raise StorageError(f"No se pudo consultar el espacio del almacén: {error}") from None
        if usage.free < self.min_free_bytes + additional_bytes:
            raise StorageError("No hay espacio libre suficiente en el almacén de imágenes.")

    def task_directory(self, task_id: str, *, create: bool = False) -> Path:
        safe_id = _uuid_text(task_id)
        if create:
            self.ensure_layout()
            directory = self.root / "staging" / safe_id
            if directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    raise StorageError("La carpeta temporal de la tarea no es segura.")
            else:
                try:
                    directory.mkdir(mode=0o750)
                except OSError as error:
                    raise StorageError(f"No se pudo crear el espacio temporal: {error}") from None
            return directory
        return self.root / "staging" / safe_id

    def published_directory(self, image_id: str) -> Path:
        return self.root / "published" / _uuid_text(image_id)

    @staticmethod
    def _safe_child(base: Path, relative: str, *, create_parent: bool = False) -> Path:
        try:
            safe_relative = validate_relative_path(relative)
        except ValueError as error:
            raise StorageError(str(error)) from None
        try:
            base_resolved = base.resolve(strict=True)
            current = base
            for part in PurePosixPath(safe_relative).parts[:-1]:
                current = current / part
                if current.exists():
                    if current.is_symlink() or not current.is_dir():
                        raise StorageError("La ruta del artefacto contiene un directorio inseguro.")
                elif create_parent:
                    current.mkdir(mode=0o750)
            parent = current.resolve(strict=True)
        except OSError as error:
            raise StorageError(f"No se pudo acceder a la ruta del artefacto: {error}") from None
        if parent != base_resolved and base_resolved not in parent.parents:
            raise StorageError("La ruta del artefacto sale del almacén.")
        path = current / PurePosixPath(safe_relative).name
        if path.exists() and path.is_symlink():
            raise StorageError("El artefacto no puede ser un enlace simbólico.")
        return path

    def artifact_path(self, task_id: str, relative: str, *, create_parent: bool = False) -> Path:
        base = self.task_directory(task_id, create=create_parent)
        if not base.is_dir() or base.is_symlink():
            raise StorageError("No existe un espacio temporal seguro para la tarea.")
        return self._safe_child(base, relative, create_parent=create_parent)

    def write_chunk(
        self,
        task_id: str,
        relative: str,
        *,
        index: int,
        offset: int,
        total_size: int,
        payload: bytes,
        sha256: str,
    ) -> bool:
        """Append one bounded chunk; return true when it was an idempotent replay."""

        if index < 0 or offset < 0:
            raise StorageError("El índice y el desplazamiento del fragmento no son válidos.")
        if not 0 < len(payload) <= self.max_chunk_bytes:
            raise StorageError("El tamaño del fragmento no es válido.")
        if total_size < 0 or total_size > self.max_image_bytes:
            raise StorageError("El tamaño declarado del artefacto no es válido.")
        if total_size and offset + len(payload) > total_size:
            raise StorageError("El fragmento supera el tamaño declarado del artefacto.")
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise StorageError("La suma del fragmento no es un SHA-256 hexadecimal.")
        if hashlib.sha256(payload).hexdigest() != sha256:
            raise StorageError("La suma del fragmento no coincide con su contenido.")
        path = self.artifact_path(task_id, relative, create_parent=True)
        try:
            if not path.exists():
                if offset != 0:
                    raise StorageError("El primer fragmento debe comenzar en cero.")
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
                os.close(descriptor)
            current_size = path.stat().st_size
            if current_size == offset + len(payload):
                with path.open("rb") as source:
                    source.seek(offset)
                    if hashlib.sha256(source.read(len(payload))).hexdigest() == sha256:
                        return True
                raise StorageError("El fragmento repetido no coincide con el contenido existente.")
            if current_size != offset:
                raise StorageError("El fragmento no continúa el artefacto de forma secuencial.")
            task_root = self.task_directory(task_id)
            stored_size = sum(
                candidate.stat().st_size
                for candidate in task_root.rglob("*")
                if candidate.is_file()
            )
            if stored_size + len(payload) > self.max_image_bytes:
                raise StorageError("Los artefactos superan el límite de tamaño de imagen.")
            self.check_capacity(len(payload))
            with path.open("r+b") as target:
                target.seek(offset)
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
        except OSError as error:
            raise StorageError(f"No se pudo escribir el artefacto: {error}") from None
        return False

    def _write_manifest(self, directory: Path, manifest: ImageManifest) -> None:
        payload = (
            json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        temporary = directory / f".manifest.{uuid.uuid4().hex}.tmp"
        final = directory / "manifest.json"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            with os.fdopen(descriptor, "wb") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(final)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as error:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise StorageError(f"No se pudo guardar el manifiesto: {error}") from None

    def verify_and_publish(self, task_id: str, image_id: str, manifest: ImageManifest) -> Path:
        """Validate all files, then move the task directory into the published namespace."""

        if str(manifest.image_id) != _uuid_text(image_id):
            raise StorageError("El manifiesto pertenece a otra imagen.")
        if not manifest.publishable:
            raise StorageError("El manifiesto no autoriza la publicación.")
        total_size = sum(artifact.size_bytes for artifact in manifest.artifacts)
        if total_size > self.max_image_bytes:
            raise StorageError("Los artefactos superan el límite de tamaño de imagen.")
        self.check_capacity()
        directory = self.task_directory(task_id)
        if not directory.is_dir() or directory.is_symlink():
            raise StorageError("No existe un espacio temporal seguro para la tarea.")
        self._write_manifest(directory, manifest)
        expected = {artifact.path for artifact in manifest.artifacts} | {"manifest.json"}
        try:
            for candidate in directory.rglob("*"):
                relative = candidate.relative_to(directory).as_posix()
                if candidate.is_symlink() or (candidate.is_file() and relative not in expected):
                    raise StorageError("El espacio temporal contiene un archivo no declarado.")
        except OSError as error:
            raise StorageError(f"No se pudo inspeccionar el espacio temporal: {error}") from None
        try:
            verify_image_artifacts(manifest, directory)
        except ValueError as error:
            raise StorageError(str(error)) from None
        destination = self.published_directory(image_id)
        if destination.exists() or destination.is_symlink():
            raise StorageError("La imagen ya tiene una publicación.")
        try:
            directory.rename(destination)
            parent_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except OSError as error:
            raise StorageError(f"No se pudo publicar la imagen atómicamente: {error}") from None
        return destination

    def published_artifact(self, image_id: str, relative: str) -> Path:
        directory = self.published_directory(image_id)
        if not directory.is_dir() or directory.is_symlink():
            raise StorageError("La publicación de la imagen no existe.")
        return self._safe_child(directory, relative)

    def status(self) -> dict[str, int | str]:
        self.ensure_layout()
        try:
            usage = shutil.disk_usage(self.root)
            staging = sum(1 for entry in (self.root / "staging").iterdir() if entry.is_dir())
            published = sum(1 for entry in (self.root / "published").iterdir() if entry.is_dir())
        except OSError as error:
            raise StorageError(f"No se pudo consultar el estado del almacén: {error}") from None
        return {
            "path": str(self.root),
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "staging_images": staging,
            "published_images": published,
        }
