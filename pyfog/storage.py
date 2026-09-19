"""Private, task-scoped storage for image artifacts."""

import contextlib
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pyfog.image_manifest import (
    ImageManifest,
    ensure_supported_extended_image,
    validate_relative_path,
    verify_image_artifacts,
)
from pyfog.transfer import (
    TransferBlock,
    TransferManifest,
    missing_block_indices,
    verified_block_indices,
    verify_complete_transfer,
    write_verified_block,
)


class StorageError(ValueError):
    """A storage request cannot be completed safely."""


SHA256_HEX_LENGTH = 64


def _uuid_text(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        msg = "El identificador de almacenamiento no es válido."
        raise StorageError(msg) from None


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
                msg = "El almacén de imágenes no es un directorio seguro."
                raise StorageError(msg)
        else:
            try:
                root.mkdir(parents=True, mode=0o750)
            except OSError as error:
                msg = f"No se pudo crear el almacén de imágenes: {error}"
                raise StorageError(msg) from None
        for name in ("staging", "published"):
            directory = root / name
            if directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    msg = f"La carpeta {name} del almacén no es segura."
                    raise StorageError(msg)
            else:
                try:
                    directory.mkdir(mode=0o750)
                except OSError as error:
                    msg = f"No se pudo preparar la carpeta {name}: {error}"
                    raise StorageError(msg) from None
        return root

    def check_capacity(self, additional_bytes: int = 0) -> None:
        if additional_bytes < 0 or additional_bytes > self.max_image_bytes:
            msg = "El tamaño de la imagen supera el límite configurado."
            raise StorageError(msg)
        try:
            usage = shutil.disk_usage(self.ensure_layout())
        except OSError as error:
            msg = f"No se pudo consultar el espacio del almacén: {error}"
            raise StorageError(msg) from None
        if usage.free < self.min_free_bytes + additional_bytes:
            msg = "No hay espacio libre suficiente en el almacén de imágenes."
            raise StorageError(msg)

    def task_directory(self, task_id: str, *, create: bool = False) -> Path:
        safe_id = _uuid_text(task_id)
        if create:
            self.ensure_layout()
            directory = self.root / "staging" / safe_id
            if directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    msg = "La carpeta temporal de la tarea no es segura."
                    raise StorageError(msg)
            else:
                try:
                    directory.mkdir(mode=0o750)
                except OSError as error:
                    msg = f"No se pudo crear el espacio temporal: {error}"
                    raise StorageError(msg) from None
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
                        msg = "La ruta del artefacto contiene un directorio inseguro."
                        raise StorageError(msg)
                elif create_parent:
                    current.mkdir(mode=0o750)
            parent = current.resolve(strict=True)
        except OSError as error:
            msg = f"No se pudo acceder a la ruta del artefacto: {error}"
            raise StorageError(msg) from None
        if parent != base_resolved and base_resolved not in parent.parents:
            msg = "La ruta del artefacto sale del almacén."
            raise StorageError(msg)
        path = current / PurePosixPath(safe_relative).name
        if path.exists() and path.is_symlink():
            msg = "El artefacto no puede ser un enlace simbólico."
            raise StorageError(msg)
        return path

    def artifact_path(self, task_id: str, relative: str, *, create_parent: bool = False) -> Path:
        base = self.task_directory(task_id, create=create_parent)
        if not base.is_dir() or base.is_symlink():
            msg = "No existe un espacio temporal seguro para la tarea."
            raise StorageError(msg)
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
            msg = "El índice y el desplazamiento del fragmento no son válidos."
            raise StorageError(msg)
        if not 0 < len(payload) <= self.max_chunk_bytes:
            msg = "El tamaño del fragmento no es válido."
            raise StorageError(msg)
        if total_size < 0 or total_size > self.max_image_bytes:
            msg = "El tamaño declarado del artefacto no es válido."
            raise StorageError(msg)
        if total_size and offset + len(payload) > total_size:
            msg = "El fragmento supera el tamaño declarado del artefacto."
            raise StorageError(msg)
        if len(sha256) != SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in sha256
        ):
            msg = "La suma del fragmento no es un SHA-256 hexadecimal."
            raise StorageError(msg)
        if hashlib.sha256(payload).hexdigest() != sha256:
            msg = "La suma del fragmento no coincide con su contenido."
            raise StorageError(msg)
        path = self.artifact_path(task_id, relative, create_parent=True)
        try:
            if not path.exists():
                if offset != 0:
                    msg = "El primer fragmento debe comenzar en cero."
                    raise StorageError(msg)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
                os.close(descriptor)
            current_size = path.stat().st_size
            if current_size == offset + len(payload):
                with path.open("rb") as source:
                    source.seek(offset)
                    if hashlib.sha256(source.read(len(payload))).hexdigest() == sha256:
                        return True
                msg = "El fragmento repetido no coincide con el contenido existente."
                raise StorageError(msg)
            if current_size != offset:
                msg = "El fragmento no continúa el artefacto de forma secuencial."
                raise StorageError(msg)
            task_root = self.task_directory(task_id)
            stored_size = sum(
                candidate.stat().st_size
                for candidate in task_root.rglob("*")
                if candidate.is_file()
            )
            if stored_size + len(payload) > self.max_image_bytes:
                msg = "Los artefactos superan el límite de tamaño de imagen."
                raise StorageError(msg)
            self.check_capacity(len(payload))
            with path.open("r+b") as target:
                target.seek(offset)
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
        except OSError as error:
            msg = f"No se pudo escribir el artefacto: {error}"
            raise StorageError(msg) from None
        return False

    def verified_blocks(self, task_id: str, relative: str, manifest: TransferManifest) -> set[int]:
        """Return verified block indexes already present in one task's staging area."""

        path = self.artifact_path(task_id, relative)
        return verified_block_indices(path, manifest)

    def missing_blocks(self, task_id: str, relative: str, manifest: TransferManifest) -> list[int]:
        """Return missing/corrupt indexes without trusting staging size alone."""

        path = self.artifact_path(task_id, relative)
        return missing_block_indices(path, manifest)

    def write_block(
        self,
        task_id: str,
        relative: str,
        *,
        manifest: TransferManifest,
        block: TransferBlock,
        payload: bytes,
    ) -> bool:
        """Persist one verified block and make retries idempotent."""

        if len(payload) > self.max_chunk_bytes:
            msg = "El bloque supera el límite configurado."
            raise StorageError(msg)
        path = self.artifact_path(task_id, relative, create_parent=True)
        try:
            if path.exists() and path.stat().st_size > manifest.size_bytes:
                msg = "El staging supera el tamaño declarado del artefacto."
                raise StorageError(msg)
            self.check_capacity(len(payload))
            return write_verified_block(path, manifest, block, payload)
        except (OSError, ValueError) as error:
            raise StorageError(str(error)) from None

    def verify_transfer(self, task_id: str, relative: str, manifest: TransferManifest) -> None:
        """Require all blocks to be verified before a manifest can publish."""

        path = self.artifact_path(task_id, relative)
        try:
            verify_complete_transfer(path, manifest)
        except (OSError, ValueError) as error:
            raise StorageError(str(error)) from None

    def _write_manifest(self, directory: Path, manifest: ImageManifest) -> None:
        payload = (
            json.dumps(
                manifest.model_dump(mode="json", exclude_none=True),
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
            msg = f"No se pudo guardar el manifiesto: {error}"
            raise StorageError(msg) from None

    def verify_and_publish(self, task_id: str, image_id: str, manifest: ImageManifest) -> Path:
        """Validate all files, then move the task directory into the published namespace."""

        if str(manifest.image_id) != _uuid_text(image_id):
            msg = "El manifiesto pertenece a otra imagen."
            raise StorageError(msg)
        if not manifest.publishable:
            msg = "El manifiesto no autoriza la publicación."
            raise StorageError(msg)
        try:
            ensure_supported_extended_image(manifest)
        except ValueError as error:
            raise StorageError(str(error)) from None
        total_size = sum(artifact.size_bytes for artifact in manifest.artifacts)
        if total_size > self.max_image_bytes:
            msg = "Los artefactos superan el límite de tamaño de imagen."
            raise StorageError(msg)
        self.check_capacity()
        directory = self.task_directory(task_id)
        if not directory.is_dir() or directory.is_symlink():
            msg = "No existe un espacio temporal seguro para la tarea."
            raise StorageError(msg)
        self._write_manifest(directory, manifest)
        expected = {artifact.path for artifact in manifest.artifacts} | {"manifest.json"}
        try:
            for candidate in directory.rglob("*"):
                relative = candidate.relative_to(directory).as_posix()
                if candidate.is_symlink() or (candidate.is_file() and relative not in expected):
                    msg = "El espacio temporal contiene un archivo no declarado."
                    raise StorageError(msg)
        except OSError as error:
            msg = f"No se pudo inspeccionar el espacio temporal: {error}"
            raise StorageError(msg) from None
        try:
            verify_image_artifacts(manifest, directory)
        except ValueError as error:
            raise StorageError(str(error)) from None
        destination = self.published_directory(image_id)
        if destination.exists() or destination.is_symlink():
            msg = "La imagen ya tiene una publicación."
            raise StorageError(msg)
        try:
            directory.rename(destination)
            parent_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except OSError as error:
            msg = f"No se pudo publicar la imagen atómicamente: {error}"
            raise StorageError(msg) from None
        return destination

    def published_artifact(self, image_id: str, relative: str) -> Path:
        directory = self.published_directory(image_id)
        if not directory.is_dir() or directory.is_symlink():
            msg = "La publicación de la imagen no existe."
            raise StorageError(msg)
        return self._safe_child(directory, relative)

    def delete_published_image(self, image_id: str) -> bool:
        """Delete exactly one UUID publication; a missing publication is already deleted."""

        directory = self.published_directory(image_id)
        published_root = self.root / "published"
        self.ensure_layout()
        if directory.parent != published_root:
            msg = "La ruta publicada de la imagen no es segura."
            raise StorageError(msg)
        if directory.is_symlink():
            msg = "La publicación de la imagen no es un directorio seguro."
            raise StorageError(msg)
        if not directory.exists():
            return False
        if not directory.is_dir():
            msg = "La publicación de la imagen no es un directorio seguro."
            raise StorageError(msg)
        try:
            shutil.rmtree(directory)
            parent_descriptor = os.open(published_root, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except OSError as error:
            msg = f"No se pudo borrar la publicación de la imagen: {error}"
            raise StorageError(msg) from None
        return True

    def remove_task_staging(self, task_id: str) -> bool:
        """Remove one task's unpublished staging directory, never a published image."""

        directory = self.task_directory(task_id)
        staging_root = self.root / "staging"
        self.ensure_layout()
        if directory == self.root or directory.parent != staging_root:
            msg = "La ruta temporal de la tarea no es segura."
            raise StorageError(msg)
        if not directory.exists():
            return False
        if directory.is_symlink() or not directory.is_dir():
            msg = "La carpeta temporal de la tarea no es segura."
            raise StorageError(msg)
        try:
            shutil.rmtree(directory)
        except OSError as error:
            msg = f"No se pudo limpiar el staging de la tarea: {error}"
            raise StorageError(msg) from None
        return True

    def staging_status(self, active_task_ids: Collection[str]) -> dict[str, list[str]]:
        """List active/orphaned temporary task directories without deleting any of them."""

        self.ensure_layout()
        active = set(active_task_ids)
        orphaned: list[str] = []
        active_entries: list[str] = []
        unsafe: list[str] = []
        try:
            entries = sorted((self.root / "staging").iterdir(), key=lambda path: path.name)
            for entry in entries:
                if entry.is_symlink() or not entry.is_dir():
                    unsafe.append(entry.name)
                    continue
                try:
                    task_id = _uuid_text(entry.name)
                except StorageError:
                    unsafe.append(entry.name)
                    continue
                if task_id in active:
                    active_entries.append(task_id)
                else:
                    orphaned.append(task_id)
        except OSError as error:
            msg = f"No se pudo inspeccionar el staging: {error}"
            raise StorageError(msg) from None
        return {"active": active_entries, "orphaned": orphaned, "unsafe": unsafe}

    def status(self) -> dict[str, int | str]:
        self.ensure_layout()
        try:
            usage = shutil.disk_usage(self.root)
            staging = sum(
                1
                for entry in (self.root / "staging").iterdir()
                if entry.is_dir() and not entry.is_symlink()
            )
            published = sum(
                1
                for entry in (self.root / "published").iterdir()
                if entry.is_dir() and not entry.is_symlink()
            )
        except OSError as error:
            msg = f"No se pudo consultar el estado del almacén: {error}"
            raise StorageError(msg) from None
        return {
            "path": str(self.root),
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "staging_images": staging,
            "published_images": published,
        }
