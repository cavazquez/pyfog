import contextlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from pyfog.agent_credentials import AgentAuthorization, credential_is_usable
from pyfog.audit import record_audit
from pyfog.coordinator import CoordinatorError, FencingLease, acquire_lease, assert_fenced
from pyfog.database import get_db
from pyfog.health import operational_metrics, operational_snapshot
from pyfog.image_manifest import (
    ensure_supported_extended_image,
    parse_image_manifest,
    validate_relative_path,
)
from pyfog.models import (
    AgentCredential,
    Host,
    Image,
    InventoryReport,
    PairingRequest,
    Task,
    TaskAttempt,
    now,
)
from pyfog.schemas import (
    Inventory,
    PairingRequestInput,
    TaskClaimInput,
    TaskHeartbeatInput,
    TaskProgressInput,
    TaskResultInput,
)
from pyfog.security import digest
from pyfog.services import ingest_inventory
from pyfog.storage import StorageError
from pyfog.tasking import (
    TaskError,
    acknowledge_task_cancellation,
    add_event,
    claim_task,
    expire_stale_tasks,
    failure_code,
    record_progress,
    touch_attempt,
    transition_task,
)

router = APIRouter()
Db = Annotated[Session, Depends(get_db)]


def bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    if not token or len(token) > 256 or "\n" in token or "\r" in token:
        return ""
    return token


def iso_utc(value: datetime) -> str:
    return f"{value.isoformat()}Z"


def coordinator_fence(request: Request, db: Session) -> FencingLease | None:
    """Require the configured active coordinator before an API mutation."""

    coordinator_id = str(getattr(request.app.state.settings, "coordinator_id", ""))
    if not coordinator_id:
        return None
    try:
        lease = acquire_lease(
            db,
            coordinator_id,
            lease_seconds=request.app.state.settings.coordinator_lease_seconds,
        )
        if lease is None:
            raise CoordinatorError("El coordinador activo no está disponible.")
        assert_fenced(db, lease)
        request.state.coordinator_lease = lease
    except CoordinatorError as error:
        db.rollback()
        raise HTTPException(503, str(error)) from None
    else:
        return lease


def get_pairing(db: Session, pairing_id: UUID) -> PairingRequest:
    pairing = db.get(PairingRequest, str(pairing_id))
    if pairing is None:
        raise HTTPException(404, "No se encontró la solicitud de registro.")
    return pairing


def authorize_pairing(request: Request, pairing: PairingRequest) -> str:
    token = bearer_token(request)
    if not token or not secrets.compare_digest(digest(token), pairing.capability_hash):
        raise HTTPException(
            401,
            "Credencial de emparejamiento inválida.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def authorize_host_agent(request: Request, db: Session, host_id: str) -> AgentAuthorization:
    token = bearer_token(request)
    host = db.get(Host, host_id)
    token_hash = digest(token)
    credential = db.scalar(
        select(AgentCredential).where(
            AgentCredential.host_id == host_id,
            AgentCredential.token_hash == token_hash,
        )
    )
    current = now()
    credential_valid = (
        credential is not None and host is not None and credential_is_usable(credential, current)
    )
    legacy_valid = (
        host is not None
        and credential is None
        and host.token_hash is not None
        and secrets.compare_digest(token_hash, host.token_hash)
        and host.token_expires_at is not None
        and host.token_expires_at > current
    )
    if not host or not token or (not credential_valid and not legacy_valid):
        raise HTTPException(
            401, "Credencial de agente inválida o vencida.", headers={"WWW-Authenticate": "Bearer"}
        )
    return AgentAuthorization(host=host, credential=credential)


def get_task(db: Session, task_id: UUID) -> Task:
    task = db.get(Task, str(task_id))
    if task is None:
        raise HTTPException(404, "No se encontró la tarea.")
    return task


def task_attempt_for_token(
    request: Request, db: Session, task_id: UUID
) -> tuple[Task, TaskAttempt]:
    task = get_task(db, task_id)
    token = bearer_token(request)
    if not token:
        raise HTTPException(
            401, "Falta la capacidad de la tarea.", headers={"WWW-Authenticate": "Bearer"}
        )
    attempt = db.scalar(
        select(TaskAttempt).where(
            TaskAttempt.task_id == task.id,
            TaskAttempt.capability_hash == digest(token),
        )
    )
    if attempt is None:
        raise HTTPException(
            401, "Capacidad de tarea inválida.", headers={"WWW-Authenticate": "Bearer"}
        )
    if attempt.agent_credential_id:
        credential = db.get(AgentCredential, attempt.agent_credential_id)
        if credential is None or not credential_is_usable(credential):
            raise HTTPException(
                401,
                "La credencial del agente fue revocada o venció.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    if task.status in {"succeeded", "failed", "cancelled", "intervention_required"}:
        raise HTTPException(409, "La tarea ya no admite eventos del agente.")
    if attempt.lease_expires_at <= now():
        expire_stale_tasks(db)
        db.commit()
        raise HTTPException(410, "La concesión de la tarea venció; requiere intervención.")
    return task, attempt


def integer_header(value: str | None, name: str) -> int:
    try:
        number = int(value or "")
    except ValueError:
        raise HTTPException(422, f"Falta un encabezado {name} válido.") from None
    return number


def artifact_route_path(value: str) -> str:
    if len(value) > 240 or not (value.startswith("partitions/") or value == "boot-sector.bin"):
        raise HTTPException(422, "La ruta del artefacto no es válida.")
    try:
        validate_relative_path(value)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return value


def task_payload(
    task: Task, attempt: TaskAttempt | None = None, token: str | None = None
) -> dict[str, Any]:
    selector = task.disk_selector
    primary_selector = selector
    disk_selectors: list[dict[str, Any]] | None = None
    if isinstance(selector, dict) and isinstance(selector.get("disks"), list):
        disk_selectors = [item for item in selector["disks"] if isinstance(item, dict)]
        if len(disk_selectors) != len(selector["disks"]):
            raise HTTPException(500, "La tarea contiene selectores de disco inválidos.")
        primary_selector = selector.get("primary") or disk_selectors[0]
    payload: dict[str, Any] = {
        "protocol_version": 1,
        "task_id": task.id,
        "operation": task.operation,
        "status": task.status,
        "phase": task.phase,
        "host_id": task.host_id,
        "image_id": task.image_id,
        "inventory_report_id": task.inventory_report_id,
        "disk": primary_selector,
        "bytes_processed": task.bytes_processed,
        "total_bytes": task.total_bytes,
        "message": task.message,
        "failure_reason": task.failure_reason,
        "cancel_requested": task.cancel_requested_at is not None,
        "created_at": iso_utc(task.created_at),
        "updated_at": iso_utc(task.updated_at),
    }
    if disk_selectors is not None:
        payload["disks"] = disk_selectors
    if attempt is not None:
        payload.update(
            {
                "attempt_id": attempt.id,
                "attempt_number": attempt.attempt_number,
                "lease_expires_at": iso_utc(attempt.lease_expires_at),
                "last_heartbeat_at": iso_utc(attempt.last_heartbeat_at),
                "last_sequence": attempt.last_sequence,
            }
        )
    if token is not None:
        payload["task_token"] = token
    return payload


@router.get("/health/live", include_in_schema=False)
def health_live() -> dict[str, str]:
    """Liveness is intentionally independent of the database and image store."""

    return {"status": "ok", "service": "pyfog"}


@router.get("/health/ready", include_in_schema=False)
def health_ready(request: Request, db: Db) -> JSONResponse:
    try:
        snapshot = operational_snapshot(db, request.app.state.artifact_store)
    except (OSError, RuntimeError, ValueError, SQLAlchemyError):
        db.rollback()
        return JSONResponse(
            {"status": "not_ready", "database": "unavailable", "storage": "unavailable"},
            status_code=503,
        )
    return JSONResponse(
        {
            "status": "ok",
            "database": snapshot["database"],
            "storage": snapshot["storage"]["status"],
            "coordinator": snapshot["coordinator"]["status"],
            "active_tasks": snapshot["active_tasks"],
        },
        status_code=200,
    )


@router.get("/health", include_in_schema=False)
def health(request: Request, db: Db) -> JSONResponse:
    """Backward-compatible readiness probe used by the container healthcheck."""

    return health_ready(request, db)


@router.get("/health/metrics", include_in_schema=False)
def health_metrics(db: Db) -> JSONResponse:
    """Expose aggregate task metrics without task IDs, messages or credentials."""

    try:
        metrics = operational_metrics(db)
    except (OSError, RuntimeError, ValueError, SQLAlchemyError):
        db.rollback()
        return JSONResponse(
            {"status": "not_ready", "metrics": "unavailable"},
            status_code=503,
        )
    return JSONResponse({"status": "ok", "metrics": metrics}, status_code=200)


@router.post("/api/v1/tasks/claim")
def claim_agent_task(payload: TaskClaimInput, request: Request, db: Db) -> JSONResponse:
    """Authorize a boot session with the host token, then issue a task-scoped capability."""

    authorization = authorize_host_agent(request, db, str(payload.host_id))
    host = authorization.host
    coordinator_lease = coordinator_fence(request, db)
    try:
        claimed = claim_task(
            db,
            host.id,
            str(payload.session_id),
            set(payload.capabilities),
            request.app.state.settings,
            agent_credential_id=(
                authorization.credential.id if authorization.credential is not None else None
            ),
            coordinator_lease=coordinator_lease,
        )
    except TaskError as error:
        raise HTTPException(409, str(error)) from None
    if claimed is None:
        return JSONResponse({"protocol_version": 1, "task": None}, status_code=200)
    task, attempt = claimed.task, claimed.attempt
    record_audit(
        db,
        actor_host_id=host.id,
        action="task.claim",
        resource_type="task",
        resource_id=task.id,
        detail=f"Intento {attempt.attempt_number} asignado al agente.",
    )
    db.commit()
    response = task_payload(task, attempt, claimed.token)
    report = db.get(InventoryReport, task.inventory_report_id)
    source_hostname = host.hostname or host.name
    system_name = host.os_name or "Ubuntu Linux"
    system_version = ""
    if report is not None and isinstance(report.data.get("os"), dict):
        source_hostname = str(report.data.get("hostname") or source_hostname)[:253]
        operating_system = report.data["os"]
        system_name = str(operating_system.get("name") or system_name)[:200]
        system_id = str(operating_system.get("id") or "ubuntu")[:200]
        system_version = str(operating_system.get("version") or "")[:200]
    else:
        system_id = "ubuntu"
    response["source"] = {
        "host_id": task.host_id,
        "inventory_report_id": report.report_id if report else "",
        "hostname": source_hostname,
    }
    response["system"] = {"name": system_name, "id": system_id, "version": system_version}
    response["lease_seconds"] = request.app.state.settings.task_lease_seconds
    response["heartbeat_seconds"] = request.app.state.settings.task_heartbeat_seconds
    response["chunk_bytes"] = request.app.state.settings.max_chunk_bytes
    if task.operation in {"restore", "clone"}:
        image = db.get(Image, task.image_id)
        if (
            image is None
            or image.deleted_at is not None
            or image.manifest_json is None
            or image.status != "ready"
        ):
            raise HTTPException(409, "La imagen ya no está disponible para restaurar.")
        response["manifest"] = image.manifest_json
        response["artifact_base"] = f"/api/v1/tasks/{task.id}/artifacts"
        source = image.manifest_json.get("source")
        system = image.manifest_json.get("system")
        if isinstance(source, dict):
            response["source"] = source
        if isinstance(system, dict):
            response["system"] = system
        response["target"] = {
            "disk": response["disk"],
            "hostname": response["disk"].get("clone_hostname", "")
            if task.operation == "clone"
            else "",
        }
        if "disks" in response:
            response["target"]["disks"] = response["disks"]
    return JSONResponse(response, status_code=200)


@router.post("/api/v1/tasks/{task_id}/heartbeat")
def heartbeat_agent_task(
    task_id: UUID, payload: TaskHeartbeatInput, request: Request, db: Db
) -> JSONResponse:
    coordinator_fence(request, db)
    task, attempt = task_attempt_for_token(request, db, task_id)
    try:
        touch_attempt(
            db,
            task,
            attempt,
            request.app.state.settings,
            phase=payload.phase or attempt.phase,
            bytes_processed=max(attempt.bytes_processed, payload.bytes_processed),
            total_bytes=(
                payload.total_bytes if payload.total_bytes is not None else attempt.total_bytes
            ),
            message=payload.message,
        )
    except TaskError as error:
        raise HTTPException(422, str(error)) from None
    db.commit()
    return JSONResponse(task_payload(task, attempt), status_code=200)


@router.post("/api/v1/tasks/{task_id}/progress")
def progress_agent_task(
    task_id: UUID, payload: TaskProgressInput, request: Request, db: Db
) -> JSONResponse:
    coordinator_fence(request, db)
    task, attempt = task_attempt_for_token(request, db, task_id)
    try:
        accepted = record_progress(
            db,
            task,
            attempt,
            request.app.state.settings,
            sequence=payload.sequence,
            phase=payload.phase,
            bytes_processed=payload.bytes_processed,
            total_bytes=payload.total_bytes,
            message=payload.message,
        )
    except TaskError as error:
        raise HTTPException(409, str(error)) from None
    db.commit()
    return JSONResponse(
        {
            "task_id": task.id,
            "attempt_id": attempt.id,
            "accepted": accepted,
            "sequence": payload.sequence,
        },
        status_code=200,
    )


@router.get("/api/v1/tasks/{task_id}/artifacts/{artifact_path:path}")
def download_task_artifact(task_id: UUID, artifact_path: str, request: Request, db: Db) -> Response:
    """Serve only a declared published artifact to the capability that owns the task."""

    task, _attempt = task_attempt_for_token(request, db, task_id)
    if task.operation == "capture":
        raise HTTPException(409, "Las tareas de captura no tienen artefactos descargables.")
    safe_path = artifact_route_path(artifact_path)
    image = db.get(Image, task.image_id)
    if (
        image is None
        or image.deleted_at is not None
        or image.status != "ready"
        or image.manifest_json is None
    ):
        raise HTTPException(409, "La imagen no está publicada y verificada.")
    try:
        manifest = parse_image_manifest(image.manifest_json)
        ensure_supported_extended_image(manifest)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    artifact = next((item for item in manifest.artifacts if item.path == safe_path), None)
    if artifact is None:
        raise HTTPException(404, "El artefacto no está declarado por la imagen.")
    try:
        path = request.app.state.artifact_store.published_artifact(image.id, safe_path)
    except StorageError as error:
        raise HTTPException(404, str(error)) from None
    offset_text = request.query_params.get("offset")
    length_text = request.query_params.get("length")
    if offset_text is not None or length_text is not None:
        try:
            offset = int(offset_text or "")
            length = int(length_text or "")
        except ValueError:
            raise HTTPException(422, "El rango del bloque no es válido.") from None
        if (
            offset < 0
            or length <= 0
            or length > request.app.state.settings.max_chunk_bytes
            or offset + length > artifact.size_bytes
        ):
            raise HTTPException(416, "El rango del bloque está fuera del artefacto.")
        try:
            with path.open("rb") as source:
                source.seek(offset)
                payload = source.read(length)
        except OSError as error:
            raise HTTPException(404, f"No se pudo leer el bloque: {error}") from None
        if len(payload) != length:
            raise HTTPException(409, "El artefacto publicado está incompleto.")
        return Response(
            payload,
            media_type="application/octet-stream",
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {offset}-{offset + length - 1}/{artifact.size_bytes}",
            },
        )
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.post("/api/v1/tasks/{task_id}/artifacts/{artifact_path:path}")
async def upload_task_artifact(
    task_id: UUID,
    artifact_path: str,
    request: Request,
    db: Db,
    chunk_index: Annotated[str | None, Header(alias="X-PyFog-Chunk-Index")] = None,
    chunk_offset: Annotated[str | None, Header(alias="X-PyFog-Chunk-Offset")] = None,
    artifact_size: Annotated[str | None, Header(alias="X-PyFog-Artifact-Size")] = None,
    chunk_sha256: Annotated[str | None, Header(alias="X-PyFog-Chunk-SHA256")] = None,
) -> JSONResponse:
    coordinator_fence(request, db)
    task, attempt = task_attempt_for_token(request, db, task_id)
    safe_path = artifact_route_path(artifact_path)
    index = integer_header(chunk_index, "X-PyFog-Chunk-Index")
    offset = integer_header(chunk_offset, "X-PyFog-Chunk-Offset")
    total_size = integer_header(artifact_size, "X-PyFog-Artifact-Size")
    if not chunk_sha256:
        raise HTTPException(422, "Falta el encabezado X-PyFog-Chunk-SHA256.")
    body = await request.body()
    if len(body) > request.app.state.settings.max_chunk_bytes:
        raise HTTPException(413, "El fragmento supera el límite configurado.")
    try:
        replayed = request.app.state.artifact_store.write_chunk(
            task.id,
            safe_path,
            index=index,
            offset=offset,
            total_size=total_size,
            payload=body,
            sha256=chunk_sha256.lower(),
        )
        touch_attempt(
            db,
            task,
            attempt,
            request.app.state.settings,
            phase="uploading",
            bytes_processed=max(attempt.bytes_processed, offset + len(body)),
            total_bytes=attempt.total_bytes,
            message=f"Recibido {safe_path}.",
        )
        db.commit()
    except (StorageError, TaskError) as error:
        db.rollback()
        raise HTTPException(409, str(error)) from None
    return JSONResponse(
        {
            "task_id": task.id,
            "attempt_id": attempt.id,
            "path": safe_path,
            "offset": offset,
            "bytes": len(body),
            "replayed": replayed,
        },
        status_code=200,
    )


def fail_agent_task(
    db: Session, task: Task, attempt: TaskAttempt, reason: str, sequence: int
) -> None:
    if task.operation in {"restore", "clone"} and attempt.started_at is not None:
        reason = f"{reason[:420]} El destino puede haber quedado incompleto; requiere diagnóstico."
    current = now()
    if sequence > attempt.last_sequence:
        attempt.last_sequence = sequence
        attempt.last_heartbeat_at = current
        add_event(
            db,
            task,
            event_type="error",
            attempt=attempt,
            sequence=sequence,
            phase="failed",
            bytes_processed=attempt.bytes_processed,
            total_bytes=attempt.total_bytes,
            failure_code=failure_code(reason),
            message=reason,
        )
    attempt.finished_at = current
    attempt.phase = "failed"
    attempt.failure_reason = reason[:500]
    transition_task(db, task, "failed", phase="failed", message=reason, failure_reason=reason)
    image = db.get(Image, task.image_id)
    if image is not None and image.status == "capturing":
        image.status = "failed"
        image.failure_reason = reason[:500]
    record_audit(
        db,
        actor_host_id=task.host_id,
        action="task.result",
        resource_type="task",
        resource_id=task.id,
        outcome="failure",
        detail="El agente informó una falla; la tarea quedó fallida.",
    )


@router.post("/api/v1/tasks/{task_id}/result")
def finish_agent_task(
    task_id: UUID, payload: TaskResultInput, request: Request, db: Db
) -> JSONResponse:
    coordinator_fence(request, db)
    task, attempt = task_attempt_for_token(request, db, task_id)
    if payload.cancelled or task.cancel_requested_at is not None:
        reason = payload.error or "El agente confirmó la cancelación cooperativa."
        try:
            acknowledge_task_cancellation(db, task, attempt, reason=reason)
        except TaskError as error:
            raise HTTPException(409, str(error)) from None
        record_audit(
            db,
            actor_host_id=task.host_id,
            action="task.cancel",
            resource_type="task",
            resource_id=task.id,
            detail="El agente reconoció la cancelación cooperativa.",
        )
        db.commit()
        if task.operation == "capture":
            # The agent has acknowledged the stop, so it is now safe to remove its
            # unpublished upload without racing a still-running writer.
            with contextlib.suppress(StorageError):
                request.app.state.artifact_store.remove_task_staging(task.id)
        return JSONResponse({"task_id": task.id, "status": task.status}, status_code=200)
    if not payload.success:
        reason = payload.error or f"El agente informó que la {task.operation} falló."
        fail_agent_task(db, task, attempt, reason, payload.sequence)
        db.commit()
        return JSONResponse({"task_id": task.id, "status": task.status}, status_code=200)
    if task.operation in {"restore", "clone"}:
        if payload.manifest is not None:
            raise HTTPException(422, "Una restauración exitosa no debe enviar un manifiesto.")
        if payload.sequence <= attempt.last_sequence:
            return JSONResponse(
                {"task_id": task.id, "status": task.status, "accepted": False}, status_code=200
            )
        image = db.get(Image, task.image_id)
        firmware_type = "uefi"
        if image is not None and isinstance(image.manifest_json, dict):
            firmware = image.manifest_json.get("firmware")
            if isinstance(firmware, dict) and firmware.get("type") in {"uefi", "bios"}:
                firmware_type = str(firmware["type"])
        try:
            record_progress(
                db,
                task,
                attempt,
                request.app.state.settings,
                sequence=payload.sequence,
                phase="verifying",
                bytes_processed=attempt.bytes_processed,
                total_bytes=attempt.total_bytes,
                message=f"El agente verificó el destino y el arranque {firmware_type.upper()}.",
            )
            attempt.finished_at = now()
            attempt.phase = "completed"
            transition_task(
                db,
                task,
                "succeeded",
                phase="completed",
                message=(
                    "La clonación terminó con identidad nueva."
                    if task.operation == "clone"
                    else "La restauración terminó y el destino fue verificado."
                ),
            )
            add_event(
                db,
                task,
                event_type="completed",
                attempt=attempt,
                sequence=attempt.last_sequence + 1,
                phase="completed",
                bytes_processed=attempt.bytes_processed,
                total_bytes=attempt.total_bytes,
                message=task.message,
            )
            attempt.last_sequence += 1
            db.commit()
        except TaskError as error:
            db.rollback()
            raise HTTPException(409, str(error)) from None
        return JSONResponse(
            {"task_id": task.id, "status": task.status, "accepted": True}, status_code=200
        )
    if payload.manifest is None:
        raise HTTPException(422, "Una captura exitosa debe incluir su manifiesto.")
    if payload.sequence <= attempt.last_sequence:
        return JSONResponse(
            {"task_id": task.id, "status": task.status, "accepted": False}, status_code=200
        )
    try:
        manifest = parse_image_manifest(payload.manifest)
        ensure_supported_extended_image(manifest)
    except ValueError as error:
        fail_agent_task(db, task, attempt, str(error), payload.sequence)
        db.commit()
        return JSONResponse({"task_id": task.id, "status": task.status}, status_code=200)
    report = db.get(InventoryReport, task.inventory_report_id)
    if (
        report is None
        or str(manifest.image_id) != task.image_id
        or str(manifest.source.host_id) != task.host_id
        or str(manifest.source.inventory_report_id) != report.report_id
    ):
        reason = "El manifiesto no coincide con la tarea, el equipo o el inventario reservado."
        fail_agent_task(db, task, attempt, reason, payload.sequence)
        db.commit()
        return JSONResponse({"task_id": task.id, "status": task.status}, status_code=200)
    try:
        accepted = record_progress(
            db,
            task,
            attempt,
            request.app.state.settings,
            sequence=payload.sequence,
            phase="verifying",
            bytes_processed=attempt.bytes_processed,
            total_bytes=attempt.total_bytes,
            message="Verificando manifiesto y artefactos antes de publicar.",
        )
        db.commit()
        if not accepted:
            return JSONResponse(
                {"task_id": task.id, "status": task.status, "accepted": False}, status_code=200
            )
        request.app.state.artifact_store.verify_and_publish(task.id, task.image_id, manifest)
    except (StorageError, TaskError, ValueError) as error:
        fail_agent_task(db, task, attempt, str(error), payload.sequence + 1)
        db.commit()
        return JSONResponse({"task_id": task.id, "status": task.status}, status_code=200)
    image = db.get(Image, task.image_id)
    if image is None:
        fail_agent_task(
            db, task, attempt, "La imagen de la tarea ya no existe.", payload.sequence + 1
        )
        db.commit()
        return JSONResponse({"task_id": task.id, "status": task.status}, status_code=200)
    image.status = "ready"
    image.manifest_image_id = str(manifest.image_id)
    image.manifest_json = manifest.model_dump(mode="json", exclude_none=True)
    image.manifest_sha256 = digest(
        json.dumps(image.manifest_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    image.source_host_id = task.host_id
    image.source_hostname = manifest.source.hostname
    image.captured_at = manifest.created_at.astimezone(UTC).replace(tzinfo=None)
    image.total_size_bytes = sum(artifact.size_bytes for artifact in manifest.artifacts)
    image.compatibility = (
        f"{manifest.system.name} · {manifest.architecture} · {manifest.firmware.type.upper()}"
    )
    image.integrity_verified_at = now()
    image.failure_reason = ""
    attempt.finished_at = now()
    attempt.phase = "completed"
    transition_task(
        db,
        task,
        "succeeded",
        phase="completed",
        message="La imagen fue verificada y publicada.",
    )
    add_event(
        db,
        task,
        event_type="completed",
        attempt=attempt,
        sequence=attempt.last_sequence + 1,
        phase="completed",
        bytes_processed=attempt.bytes_processed,
        total_bytes=attempt.total_bytes,
        message=task.message,
    )
    attempt.last_sequence += 1
    record_audit(
        db,
        actor_host_id=task.host_id,
        action="image.publish",
        resource_type="image",
        resource_id=image.id,
        detail="Manifiesto y artefactos verificados y publicados.",
    )
    db.commit()
    return JSONResponse(
        {"task_id": task.id, "status": task.status, "accepted": True}, status_code=200
    )


@router.post("/api/v1/hosts/{host_id}/inventory", status_code=201)
def receive_inventory(
    request: Request, host_id: UUID, inventory: Inventory, db: Db
) -> JSONResponse:
    host = authorize_host_agent(request, db, str(host_id)).host
    coordinator_fence(request, db)
    try:
        report, created = ingest_inventory(db, host, inventory, "api")
    except HTTPException:
        db.rollback()
        record_audit(
            db,
            actor_host_id=host.id,
            action="inventory.receive",
            resource_type="host",
            resource_id=host.id,
            outcome="failure",
            detail="El informe API fue rechazado por las reglas de inventario.",
        )
        db.commit()
        raise
    record_audit(
        db,
        actor_host_id=host.id,
        action="inventory.receive",
        resource_type="host",
        resource_id=host.id,
        detail="Informe API recibido o repetido de forma idempotente.",
    )
    db.commit()
    return JSONResponse(
        {"host_id": host.id, "report_id": report.report_id, "created": created},
        status_code=201 if created else 200,
    )


@router.post("/api/v1/pairing/requests", status_code=202)
def create_pairing_request(payload: PairingRequestInput, request: Request, db: Db) -> JSONResponse:
    """Create a short-lived discovery request without granting host permissions."""

    coordinator_fence(request, db)
    current = now()
    db.execute(
        update(PairingRequest)
        .where(PairingRequest.status == "pending", PairingRequest.expires_at <= current)
        .values(status="expired", challenge=None)
    )
    existing = db.scalar(
        select(PairingRequest).where(PairingRequest.session_id == str(payload.session_id))
    )
    if existing:
        raise HTTPException(409, "La sesión PXE ya tiene una solicitud; iniciá otra sesión.")
    pending = (
        db.scalar(
            select(func.count())
            .select_from(PairingRequest)
            .where(
                PairingRequest.mac_address == payload.mac_address,
                PairingRequest.status == "pending",
                PairingRequest.expires_at > current,
            )
        )
        or 0
    )
    if pending >= 5:
        db.rollback()
        raise HTTPException(429, "Hay demasiadas solicitudes pendientes para esta MAC.")
    capability = secrets.token_urlsafe(32)
    expires_at = current + timedelta(seconds=request.app.state.settings.pairing_seconds)
    pairing = PairingRequest(
        session_id=str(payload.session_id),
        mac_address=payload.mac_address,
        challenge=payload.challenge,
        challenge_hash=digest(payload.challenge),
        status="pending",
        expires_at=expires_at,
        capability_hash=digest(capability),
        rejection_reason="",
    )
    db.add(pairing)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409, "La sesión PXE ya tiene una solicitud; iniciá otra sesión."
        ) from None
    db.refresh(pairing)
    return JSONResponse(
        {
            "request_id": pairing.id,
            "session_id": pairing.session_id,
            "mac_address": pairing.mac_address,
            "challenge": payload.challenge,
            "poll_token": capability,
            "expires_at": iso_utc(pairing.expires_at),
        },
        status_code=202,
    )


@router.get("/api/v1/pairing/requests/{pairing_id}")
def pairing_request_status(request: Request, pairing_id: UUID, db: Db) -> JSONResponse:
    pairing = get_pairing(db, pairing_id)
    capability = authorize_pairing(request, pairing)
    current = now()
    if pairing.status in {"pending", "approved"} and pairing.expires_at <= current:
        pairing.status, pairing.challenge = "expired", None
        db.commit()
    response: dict[str, str] = {
        "request_id": pairing.id,
        "session_id": pairing.session_id,
        "status": pairing.status,
        "mac_address": pairing.mac_address,
        "expires_at": iso_utc(pairing.expires_at),
    }
    if pairing.status == "approved" and pairing.host_id:
        response["host_id"] = pairing.host_id
        if pairing.capability_used_at is None:
            # The same one-time capability authenticates polling and inventory submission. It is
            # returned only after approval and disappears after the first accepted report.
            response["inventory_token"] = capability
    if pairing.status == "rejected" and pairing.rejection_reason:
        response["reason"] = pairing.rejection_reason
    return JSONResponse(response)


@router.post("/api/v1/pairing/requests/{pairing_id}/inventory", status_code=201)
def receive_pairing_inventory(
    request: Request, pairing_id: UUID, inventory: Inventory, db: Db
) -> JSONResponse:
    pairing = get_pairing(db, pairing_id)
    authorize_pairing(request, pairing)
    coordinator_fence(request, db)
    current = now()
    if pairing.status == "pending" and pairing.expires_at <= current:
        pairing.status, pairing.challenge = "expired", None
        db.commit()
    if pairing.status != "approved" or not pairing.host_id:
        raise HTTPException(410, "La solicitud PXE no está autorizada para recibir inventario.")
    if pairing.expires_at <= current and pairing.capability_used_at is None:
        pairing.status, pairing.challenge = "expired", None
        db.commit()
        raise HTTPException(410, "La capacidad de inventario venció.")
    if pairing.capability_used_at is not None:
        if pairing.last_report_id != str(inventory.report_id):
            raise HTTPException(401, "La capacidad de inventario ya fue utilizada.")
        report = db.scalar(
            select(InventoryReport).where(
                InventoryReport.host_id == pairing.host_id,
                InventoryReport.report_id == str(inventory.report_id),
            )
        )
        if report is None:
            raise HTTPException(409, "No se encontró el informe previo de esta sesión.")
        return JSONResponse(
            {"host_id": pairing.host_id, "report_id": report.report_id, "created": False},
            status_code=200,
        )
    host = db.get(Host, pairing.host_id)
    if host is None:
        raise HTTPException(409, "El equipo asociado a la solicitud ya no existe.")
    report, created = ingest_inventory(db, host, inventory, "pxe")
    pairing.capability_used_at = now()
    pairing.last_report_id = report.report_id
    db.commit()
    return JSONResponse(
        {"host_id": host.id, "report_id": report.report_id, "created": created},
        status_code=201 if created else 200,
    )
