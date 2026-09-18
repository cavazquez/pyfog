import contextlib
import secrets
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from pyfog.agent_credentials import issue_credential, revoke_credentials
from pyfog.audit import record_audit
from pyfog.config import PACKAGE_DIR
from pyfog.content import NAVIGATION, SHELL_COPY
from pyfog.database import get_db
from pyfog.health import operational_snapshot
from pyfog.image_catalog import IMAGE_STATUSES, image_is_selectable
from pyfog.image_manifest import (
    ImageManifest,
    image_compatibility_errors,
    parse_image_manifest,
)
from pyfog.models import (
    AuditEvent,
    Host,
    Image,
    InventoryReport,
    LoginSession,
    PairingRequest,
    Task,
    TaskAttempt,
    TaskEvent,
    User,
    now,
)
from pyfog.rbac import ROLE_LABELS, ROLES, has_permission, is_role
from pyfog.schemas import CloneInput, HostInput, ImageInput, Inventory
from pyfog.security import (
    authenticate,
    csrf_token,
    digest,
    require_permission,
    require_user,
    verify_csrf,
)
from pyfog.services import ingest_inventory
from pyfog.storage import StorageError
from pyfog.tasking import (
    ACTIVE_TASK_STATES,
    TASK_LABELS,
    TASK_OPERATION_LABELS,
    TaskError,
    add_event,
    expire_stale_tasks,
    reconcile_task,
    request_task_cancellation,
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
Db = Annotated[Session, Depends(get_db)]


class Flash(TypedDict):
    level: Literal["success"]
    message: str


IMAGE_STATUS_LABELS = {
    "draft": "Borrador",
    "capturing": "Capturando",
    "ready": "Lista",
    "failed": "Fallida",
    "deleted": "Eliminada",
}


def format_bytes(value: int | None) -> str:
    if value is None:
        return "No disponible"
    size = float(value)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]:
        if size < 1024 or unit == "PiB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return "No disponible"


def format_date(value: datetime | None) -> str:
    return value.strftime("%d/%m/%Y · %H:%M UTC") if value else "Todavía sin inventario"


templates.env.filters["bytes"] = format_bytes
templates.env.filters["date"] = format_date


def current_section(path: str) -> str:
    for item in NAVIGATION:
        if path == item.href or path.startswith(f"{item.href}/"):
            return item.key
    return "hosts"


def set_flash(request: Request, message: str) -> None:
    request.session["flash"] = {"level": "success", "message": message}


def take_flash(request: Request) -> Flash | None:
    value = request.session.pop("flash", None)
    if not isinstance(value, dict) or value.get("level") != "success":
        return None
    message = value.get("message")
    if not isinstance(message, str):
        return None
    return {"level": "success", "message": message}


def render(request: Request, template: str, *, status: int = 200, **context: Any) -> Response:
    user = context.get("user")
    section = context.pop("section", current_section(request.url.path))
    navigation = tuple(
        item for item in NAVIGATION if user is not None and has_permission(user, item.permission)
    )
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "csrf": csrf_token(request),
            "flash": take_flash(request),
            "navigation": navigation,
            "shell": SHELL_COPY,
            "section": section,
            "can": lambda permission: has_permission(user, permission),
            "role_labels": ROLE_LABELS,
            **context,
        },
        status_code=status,
    )


def get_host(db: Session, host_id: UUID) -> Host:
    host = db.get(Host, str(host_id))
    if host is None:
        raise HTTPException(404, "No se encontró el equipo.")
    return host


def get_pairing_request(db: Session, pairing_id: UUID) -> PairingRequest:
    pairing = db.get(PairingRequest, str(pairing_id))
    if pairing is None:
        raise HTTPException(404, "No se encontró la solicitud de registro.")
    return pairing


def get_image(db: Session, image_id: UUID) -> Image:
    image = db.get(Image, str(image_id))
    if image is None:
        raise HTTPException(404, "No se encontró la imagen.")
    return image


def image_status_label(image: Image) -> str:
    return (
        "Eliminada"
        if image.deleted_at is not None
        else IMAGE_STATUS_LABELS.get(image.status, image.status)
    )


def latest_report(db: Session, host: Host) -> InventoryReport | None:
    return db.scalar(
        select(InventoryReport)
        .where(InventoryReport.host_id == host.id)
        .order_by(InventoryReport.collected_at.desc(), InventoryReport.received_at.desc())
        .limit(1)
    )


def inventory_disks(report: InventoryReport | None) -> list[dict[str, Any]]:
    if report is None:
        return []
    try:
        inventory = Inventory.model_validate(report.data)
    except ValidationError:
        return []
    disks: list[dict[str, Any]] = []
    for disk in inventory.disks:
        values = disk.model_dump(mode="json")
        if disk.wwn:
            stable_id = f"wwn:{disk.wwn}"
        elif disk.serial_number:
            stable_id = f"serial:{disk.serial_number}"
        else:
            stable_id = f"path:{disk.name}"
        values["stable_id"] = stable_id
        disks.append(values)
    return disks


def task_status_payload(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "status": task.status,
        "status_label": TASK_LABELS.get(task.status, task.status),
        "phase": task.phase,
        "bytes_processed": task.bytes_processed,
        "total_bytes": task.total_bytes,
        "message": task.message,
        "failure_reason": task.failure_reason,
        "updated_at": iso_task_date(task.updated_at),
    }


def iso_task_date(value: datetime) -> str:
    return f"{value.isoformat()}Z"


def task_disk_selector(
    disk: dict[str, Any],
    *,
    operation: str = "capture",
    manifest: ImageManifest | None = None,
    clone_hostname: str = "",
) -> dict[str, Any]:
    selector: dict[str, Any] = {
        key: disk[key]
        for key in (
            "stable_id",
            "name",
            "size_bytes",
            "model",
            "serial_number",
            "wwn",
            "transport",
            "logical_sector_bytes",
            "removable",
        )
        if key in disk
    }
    if operation in {"restore", "clone"} and manifest is not None:
        selector.update(
            {
                "operation": operation,
                "source_size_bytes": manifest.disk.size_bytes,
                "required_logical_sector_bytes": manifest.disk.logical_sector_bytes,
            }
        )
        if operation == "clone":
            selector["clone_hostname"] = clone_hostname
    return selector


def selectable_manifest(request: Request, image: Image) -> ImageManifest | None:
    """Return a parsed, published manifest or None for a stale catalog row."""

    if not image_is_selectable(image) or image.manifest_json is None:
        return None
    try:
        manifest = parse_image_manifest(image.manifest_json)
        if str(manifest.image_id) != image.id:
            return None
        if image_compatibility_errors(manifest):
            return None
        directory = request.app.state.artifact_store.published_directory(image.id)
        if not directory.is_dir() or directory.is_symlink():
            return None
        for artifact in manifest.artifacts:
            path = request.app.state.artifact_store.published_artifact(image.id, artifact.path)
            if not path.is_file() or path.is_symlink():
                return None
        return manifest
    except (StorageError, ValueError, OSError):
        return None


def deployment_validation(
    image: Image,
    manifest: ImageManifest | None,
    host: Host,
    disk: dict[str, Any] | None,
    *,
    operation: str,
    clone_hostname: str = "",
) -> dict[str, str]:
    errors: dict[str, str] = {}
    if manifest is None:
        errors["image_id"] = "La imagen no está publicada, verificada o disponible."
        return errors
    source_host_id = str(manifest.source.host_id)
    if image.id is not None and str(manifest.image_id) != image.id:
        errors["image_id"] = "La imagen publicada no coincide con su manifiesto."
    if image.source_host_id and image.source_host_id != source_host_id:
        errors["image_id"] = "La imagen publicada no coincide con su equipo de origen."
    compatibility_errors = image_compatibility_errors(manifest)
    if compatibility_errors:
        errors["image_id"] = "La imagen no es compatible con el perfil actual: " + "; ".join(
            compatibility_errors
        )
    if operation == "restore" and source_host_id != host.id:
        errors["image_id"] = "La restauración sólo puede volver al equipo de origen."
    if operation == "clone" and source_host_id == host.id:
        errors["image_id"] = "La clonación necesita un equipo destino diferente del origen."
    if disk is None:
        errors["disk_key"] = "Seleccioná un disco del último inventario."
    else:
        size = int(disk.get("size_bytes") or 0)
        required_size = manifest.disk.size_bytes
        if size < required_size:
            errors["disk_key"] = "El disco destino es menor que el de la imagen."
        sector = disk.get("logical_sector_bytes")
        if sector != manifest.disk.logical_sector_bytes:
            errors["disk_key"] = "El sector lógico del destino no es compatible con la imagen."
        if disk.get("removable"):
            errors["disk_key"] = "No se puede usar un disco removible como destino."
    if operation == "clone":
        try:
            CloneInput.model_validate({"hostname": clone_hostname})
        except ValidationError as error:
            errors["hostname"] = str(error.errors()[0]["msg"])
        else:
            if clone_hostname.lower() == manifest.source.hostname.lower():
                errors["hostname"] = "El hostname del clon debe ser diferente del origen."
    return errors


def validation_message(error: ValidationError) -> str:
    parts = []
    for item in error.errors(include_input=False, include_url=False)[:5]:
        field = ".".join(str(part) for part in item["loc"])
        parts.append(f"{field}: {item['msg']}")
    return "Revisá el informe. " + "; ".join(parts)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    return render(request, "login.html", error="")


@router.post("/login")
async def login(request: Request, db: Db) -> Response:
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    username, password = str(form.get("username", "")), str(form.get("password", ""))
    if len(username) > 100 or len(password) > 1024:
        raise HTTPException(400, "Credenciales inválidas.")
    user = authenticate(request, db, username, password)
    if user is None:
        return render(request, "login.html", error="Usuario o contraseña incorrectos.", status=401)
    set_flash(request, "Sesión iniciada.")
    return RedirectResponse("/hosts", 303)


@router.post("/logout")
async def logout(request: Request, db: Db) -> Response:
    require_user(request, db)
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    db.execute(
        delete(LoginSession).where(LoginSession.token_hash == digest(request.session["login"]))
    )
    db.commit()
    request.session.clear()
    set_flash(request, "Sesión cerrada.")
    return RedirectResponse("/login", 303)


@router.get("/")
def home() -> Response:
    return RedirectResponse("/hosts", 303)


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request, db: Db) -> Response:
    user = require_permission(request, db, "status.read")
    snapshot: dict[str, Any] | None
    try:
        snapshot = operational_snapshot(db, request.app.state.artifact_store)
    except Exception:  # pragma: no cover - deployment failure path
        db.rollback()
        snapshot = None
    return render(
        request,
        "status.html",
        user=user,
        snapshot=snapshot,
        section="status",
        status=200 if snapshot is not None else 503,
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_list(
    request: Request,
    db: Db,
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
) -> Response:
    user = require_permission(request, db, "audit.read")
    query = select(AuditEvent)
    matching = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, (matching + 49) // 50)
    page = min(page, pages)
    events = db.scalars(
        query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .offset((page - 1) * 50)
        .limit(50)
    ).all()
    user_ids = {event.actor_user_id for event in events if event.actor_user_id is not None}
    host_ids = {event.actor_host_id for event in events if event.actor_host_id}
    users = (
        {
            actor.id: actor.username
            for actor in db.scalars(select(User).where(User.id.in_(user_ids)))
        }
        if user_ids
        else {}
    )
    hosts = (
        {actor.id: actor.name for actor in db.scalars(select(Host).where(Host.id.in_(host_ids)))}
        if host_ids
        else {}
    )
    return render(
        request,
        "audit.html",
        user=user,
        events=events,
        audit_users=users,
        audit_hosts=hosts,
        matching=matching,
        page=page,
        pages=pages,
        section="audit",
    )


@router.get("/users", response_class=HTMLResponse)
def user_list(request: Request, db: Db) -> Response:
    user = require_permission(request, db, "users.manage")
    users = db.scalars(select(User).order_by(User.username.asc())).all()
    return render(
        request,
        "users.html",
        user=user,
        users=users,
        roles=ROLES,
        role_labels=ROLE_LABELS,
        section="users",
    )


@router.post("/users/{user_id}/role")
async def user_role_update(request: Request, user_id: int, db: Db) -> Response:
    user = require_permission(
        request, db, "users.manage", resource_type="user", resource_id=str(user_id)
    )
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "No se encontró el usuario.")
    role = str(form.get("role", ""))
    if not is_role(role):
        raise HTTPException(422, "El rol seleccionado no es válido.")
    if target.role == "admin" and role != "admin":
        admin_count = (
            db.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0
        )
        if admin_count <= 1:
            record_audit(
                db,
                actor_user_id=user.id,
                action="user.role.update",
                resource_type="user",
                resource_id=str(target.id),
                outcome="failure",
                reason="No se puede quitar el último administrador.",
                detail="Cambio de rol rechazado para conservar acceso administrativo.",
            )
            db.commit()
            raise HTTPException(409, "Debe existir al menos un administrador.")
    previous_role = target.role
    target.role = role
    record_audit(
        db,
        actor_user_id=user.id,
        action="user.role.update",
        resource_type="user",
        resource_id=str(target.id),
        reason=f"Rol cambiado de {previous_role} a {role}.",
        detail="Rol de usuario actualizado sin modificar credenciales.",
    )
    db.commit()
    set_flash(request, f"Rol de {target.username} actualizado a {ROLE_LABELS[role]}.")
    return RedirectResponse("/users", 303)


@router.get("/images", response_class=HTMLResponse)
def image_list(
    request: Request,
    db: Db,
    q: Annotated[str, Query(max_length=200)] = "",
    image_status: Annotated[
        str, Query(alias="status", pattern="^(all|draft|capturing|ready|failed|deleted)$")
    ] = "all",
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
) -> Response:
    user = require_permission(request, db, "images.read")
    query = select(Image)
    if q.strip():
        term = q.strip()
        query = query.where(
            or_(
                Image.name.icontains(term, autoescape=True),
                Image.description.icontains(term, autoescape=True),
            )
        )
    if image_status == "deleted":
        query = query.where(Image.deleted_at.is_not(None))
    elif image_status != "all":
        query = query.where(Image.status == image_status)
        query = query.where(Image.deleted_at.is_(None))
    matching = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = min(page, max(1, (matching + 19) // 20))
    images = db.scalars(query.order_by(Image.name.asc()).offset((page - 1) * 20).limit(20)).all()
    source_ids = {image.source_host_id for image in images if image.source_host_id}
    hosts = (
        {
            host.id: host.name
            for host in db.scalars(select(Host).where(Host.id.in_(source_ids))).all()
        }
        if source_ids
        else {}
    )
    return render(
        request,
        "images.html",
        user=user,
        images=images,
        source_hosts=hosts,
        status_labels=IMAGE_STATUS_LABELS,
        statuses=IMAGE_STATUSES,
        q=q,
        image_status=image_status,
        page=page,
        pages=max(1, (matching + 19) // 20),
        matching=matching,
        section="images",
    )


@router.get("/images/new", response_class=HTMLResponse)
def image_new(request: Request, db: Db) -> Response:
    return render(
        request,
        "image_form.html",
        user=require_permission(request, db, "images.create"),
        values={},
        errors={},
    )


@router.post("/images/new")
async def image_create(request: Request, db: Db) -> Response:
    user = require_permission(request, db, "images.create")
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    values = {key: str(form.get(key, "")) for key in ("name", "description")}
    errors: dict[str, str] = {}
    try:
        data = ImageInput.model_validate(values)
    except ValidationError as error:
        errors = {str(e["loc"][0]): e["msg"] for e in error.errors()}
    else:
        image = Image(**data.model_dump())
        db.add(image)
        try:
            db.flush()
            record_audit(
                db,
                actor_user_id=user.id,
                action="image.create",
                resource_type="image",
                resource_id=image.id,
                detail="Ficha de imagen creada como borrador.",
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            errors["name"] = "Ya existe una imagen con ese nombre."
        else:
            set_flash(request, "Imagen creada como borrador.")
            return RedirectResponse(f"/images/{image.id}", 303)
    return render(request, "image_form.html", user=user, values=values, errors=errors, status=422)


@router.get("/images/{image_id}", response_class=HTMLResponse)
def image_detail(request: Request, image_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "images.read", resource_type="image", resource_id=str(image_id)
    )
    image = get_image(db, image_id)
    source_host = db.get(Host, image.source_host_id) if image.source_host_id else None
    return render(
        request,
        "image_detail.html",
        user=user,
        image=image,
        source_host=source_host,
        status_label=image_status_label(image),
        selectable=image_is_selectable(image),
        section="images",
    )


@router.get("/images/{image_id}/edit", response_class=HTMLResponse)
def image_edit(request: Request, image_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "images.update", resource_type="image", resource_id=str(image_id)
    )
    image = get_image(db, image_id)
    if image.deleted_at is not None:
        raise HTTPException(409, "Una imagen eliminada conserva su historial y no se puede editar.")
    return render(
        request,
        "image_form.html",
        user=user,
        image=image,
        values={"name": image.name, "description": image.description},
        errors={},
        section="images",
    )


@router.post("/images/{image_id}/edit")
async def image_update(request: Request, image_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "images.update", resource_type="image", resource_id=str(image_id)
    )
    image = get_image(db, image_id)
    if image.deleted_at is not None:
        raise HTTPException(409, "Una imagen eliminada conserva su historial y no se puede editar.")
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    values = {key: str(form.get(key, "")) for key in ("name", "description")}
    errors: dict[str, str] = {}
    try:
        data = ImageInput.model_validate(values)
    except ValidationError as error:
        errors = {str(e["loc"][0]): e["msg"] for e in error.errors()}
    else:
        image.name, image.description = data.name, data.description
        try:
            db.flush()
            record_audit(
                db,
                actor_user_id=user.id,
                action="image.update",
                resource_type="image",
                resource_id=image.id,
                detail="Ficha de imagen actualizada.",
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            errors["name"] = "Ya existe una imagen con ese nombre."
        else:
            set_flash(request, "Imagen actualizada.")
            return RedirectResponse(f"/images/{image.id}", 303)
    return render(
        request,
        "image_form.html",
        user=user,
        image=image,
        values=values,
        errors=errors,
        status=422,
        section="images",
    )


@router.post("/images/{image_id}/delete")
async def image_delete(request: Request, image_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "images.delete", resource_type="image", resource_id=str(image_id)
    )
    image = get_image(db, image_id)
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    confirmation = str(form.get("confirm_name", "")).strip()
    if confirmation != image.name:
        raise HTTPException(422, "Escribí el nombre exacto de la imagen para confirmar el borrado.")
    if image.deleted_at is not None:
        set_flash(request, "La imagen ya estaba eliminada; su historial se conserva.")
        return RedirectResponse(f"/images/{image.id}", 303)
    if db.scalar(
        select(Task.id)
        .where(Task.image_id == image.id, Task.status.in_(ACTIVE_TASK_STATES))
        .limit(1)
    ):
        raise HTTPException(409, "No se puede borrar una imagen con una tarea activa.")
    try:
        request.app.state.artifact_store.delete_published_image(image.id)
    except StorageError as error:
        raise HTTPException(409, str(error)) from None
    image.deleted_at = now()
    image.integrity_verified_at = None
    record_audit(
        db,
        actor_user_id=user.id,
        action="image.delete",
        resource_type="image",
        resource_id=image.id,
        detail="Publicación eliminada; fila histórica conservada.",
    )
    db.commit()
    set_flash(
        request,
        "Imagen eliminada. Se conservó su historial y no se puede volver a desplegar.",
    )
    return RedirectResponse(f"/images/{image.id}", 303)


@router.get("/hosts/{host_id}/capture", response_class=HTMLResponse)
def capture_page(request: Request, host_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "images.capture", resource_type="host", resource_id=str(host_id)
    )
    host = get_host(db, host_id)
    report = latest_report(db, host)
    if report is None:
        raise HTTPException(409, "El equipo necesita un inventario antes de capturar una imagen.")
    disks = inventory_disks(report)
    images = db.scalars(
        select(Image)
        .where(Image.status == "draft", Image.deleted_at.is_(None))
        .order_by(Image.name.asc())
    ).all()
    return render(
        request,
        "capture.html",
        user=user,
        host=host,
        report=report,
        disks=disks,
        images=images,
        values={"idempotency_key": secrets.token_urlsafe(18)},
        errors={},
        section="hosts",
    )


@router.post("/hosts/{host_id}/capture")
async def capture_request(request: Request, host_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "images.capture", resource_type="host", resource_id=str(host_id)
    )
    host = get_host(db, host_id)
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    values = {
        key: str(form.get(key, "")).strip()
        for key in ("image_id", "image_name", "image_description", "disk_key", "idempotency_key")
    }
    values["confirm"] = str(form.get("confirm", ""))
    errors: dict[str, str] = {}
    report = latest_report(db, host)
    disks = inventory_disks(report)
    images = db.scalars(
        select(Image)
        .where(Image.status == "draft", Image.deleted_at.is_(None))
        .order_by(Image.name.asc())
    ).all()
    if report is None:
        errors["form"] = "El equipo necesita un inventario antes de capturar una imagen."
    if values["confirm"] != "1":
        errors["confirm"] = "Confirmá el equipo, el disco y la imagen antes de iniciar."
    selected_disk = next(
        (disk for disk in disks if disk.get("stable_id") == values["disk_key"]), None
    )
    if selected_disk is None:
        errors["disk_key"] = "Seleccioná un disco del último inventario."
    image: Image | None = None
    if values["image_id"]:
        try:
            image = get_image(db, UUID(values["image_id"]))
        except (ValueError, HTTPException):
            errors["image_id"] = "La ficha de imagen seleccionada no es válida."
        else:
            if image.status != "draft" or image.deleted_at is not None:
                errors["image_id"] = "Sólo se puede capturar sobre una ficha en borrador."
    else:
        try:
            image_data = ImageInput.model_validate(
                {"name": values["image_name"], "description": values["image_description"]}
            )
        except ValidationError as error:
            errors["image_name"] = str(error.errors()[0]["msg"])
        else:
            image = Image(**image_data.model_dump())
    if not values["idempotency_key"]:
        values["idempotency_key"] = digest(
            ":".join(
                [
                    str(user.id),
                    host.id,
                    values["image_id"] or values["image_name"],
                    values["disk_key"],
                    report.id if report else "",
                ]
            )
        )
    elif len(values["idempotency_key"]) > 128:
        errors["form"] = "La solicitud no tiene un identificador válido."
    existing = db.scalar(select(Task).where(Task.idempotency_key == values["idempotency_key"]))
    if existing is not None:
        if existing.requested_by != user.id:
            errors["form"] = "La solicitud ya está siendo utilizada."
        else:
            return RedirectResponse(f"/tasks/{existing.id}", 303)
    if db.scalar(select(Task.id).where(Task.reservation_key == host.id)) is not None:
        errors["form"] = "Este equipo ya tiene una tarea activa."
    if errors or report is None or selected_disk is None or image is None:
        return render(
            request,
            "capture.html",
            user=user,
            host=host,
            report=report,
            disks=disks,
            images=images,
            values=values,
            errors=errors,
            status=422,
            section="hosts",
        )
    try:
        request.app.state.artifact_store.check_capacity()
    except StorageError as error:
        return render(
            request,
            "capture.html",
            user=user,
            host=host,
            report=report,
            disks=disks,
            images=images,
            values=values,
            errors={"form": str(error)},
            status=409,
            section="hosts",
        )
    if image.id is None:
        db.add(image)
        db.flush()
    image_id = image.id
    image.status = "capturing"
    image.source_host_id = host.id
    image.source_hostname = host.hostname
    task = Task(
        operation="capture",
        status="approved",
        requested_by=user.id,
        host_id=host.id,
        image_id=image_id,
        inventory_report_id=report.id,
        disk_selector=task_disk_selector(selected_disk),
        idempotency_key=values["idempotency_key"],
        reservation_key=host.id,
        phase="queued",
        bytes_processed=0,
        total_bytes=int(selected_disk["size_bytes"]),
        message="En espera de un agente PXE compatible.",
    )
    db.add(image)
    db.add(task)
    db.flush()
    add_event(
        db,
        task,
        event_type="created",
        phase="queued",
        total_bytes=task.total_bytes,
        message=task.message,
    )
    try:
        db.flush()
        record_audit(
            db,
            actor_user_id=user.id,
            action="task.create",
            resource_type="task",
            resource_id=task.id,
            detail="Tarea de captura creada.",
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        record_audit(
            db,
            actor_user_id=user.id,
            action="task.create",
            resource_type="host",
            resource_id=host.id,
            outcome="failure",
            detail="No se pudo reservar el equipo para la captura.",
        )
        db.commit()
        return render(
            request,
            "capture.html",
            user=user,
            host=host,
            report=report,
            disks=disks,
            images=images,
            values=values,
            errors={"form": "No se pudo reservar el equipo; probá nuevamente."},
            status=409,
            section="hosts",
        )
    set_flash(request, "Captura encolada. Arrancá el equipo por PXE para iniciar el análisis.")
    return RedirectResponse(f"/tasks/{task.id}", 303)


def deployment_images(db: Session, host: Host, operation: str) -> list[Image]:
    query = select(Image).where(Image.status == "ready", Image.deleted_at.is_(None))
    if operation == "restore":
        query = query.where(Image.source_host_id == host.id)
    else:
        query = query.where(Image.source_host_id != host.id)
    return list(db.scalars(query.order_by(Image.name.asc())).all())


def deployment_page(request: Request, host_id: UUID, db: Session, *, operation: str) -> Response:
    permission = "restore.execute" if operation == "restore" else "clone.execute"
    user = require_permission(
        request, db, permission, resource_type="host", resource_id=str(host_id)
    )
    host = get_host(db, host_id)
    report = latest_report(db, host)
    return render(
        request,
        "deployment.html",
        user=user,
        host=host,
        report=report,
        disks=inventory_disks(report),
        images=deployment_images(db, host, operation),
        operation=operation,
        operation_label=TASK_OPERATION_LABELS[operation],
        values={"idempotency_key": secrets.token_urlsafe(18)},
        errors={},
        section="hosts",
    )


async def deployment_request(
    request: Request, host_id: UUID, db: Session, *, operation: str
) -> Response:
    permission = "restore.execute" if operation == "restore" else "clone.execute"
    user = require_permission(
        request, db, permission, resource_type="host", resource_id=str(host_id)
    )
    host = get_host(db, host_id)
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    values = {
        key: str(form.get(key, "")).strip()
        for key in ("image_id", "disk_key", "idempotency_key", "hostname")
    }
    values["confirm"] = str(form.get("confirm", ""))
    errors: dict[str, str] = {}
    report = latest_report(db, host)
    disks = inventory_disks(report)
    images = deployment_images(db, host, operation)
    selected_disk = next(
        (disk for disk in disks if disk.get("stable_id") == values["disk_key"]), None
    )
    if report is None:
        errors["form"] = "El equipo necesita un inventario actualizado antes de operar."
    if values["confirm"] != "1":
        errors["confirm"] = "Confirmá el equipo, el disco y la sobrescritura antes de iniciar."
    image: Image | None = None
    manifest: ImageManifest | None = None
    if values["image_id"]:
        try:
            image = get_image(db, UUID(values["image_id"]))
        except (ValueError, HTTPException):
            errors["image_id"] = "La imagen seleccionada no es válida."
        else:
            manifest = selectable_manifest(request, image)
            if manifest is None or not any(candidate.id == image.id for candidate in images):
                errors["image_id"] = "La imagen no está disponible para esta operación."
    else:
        errors["image_id"] = "Seleccioná una imagen publicada y verificada."
    if operation == "clone" and values["hostname"]:
        with contextlib.suppress(ValidationError):
            values["hostname"] = CloneInput.model_validate(
                {"hostname": values["hostname"]}
            ).hostname
    errors.update(
        {
            key: value
            for key, value in deployment_validation(
                image or Image(name="invalid"),
                manifest,
                host,
                selected_disk,
                operation=operation,
                clone_hostname=values["hostname"],
            ).items()
            if key not in errors
        }
    )
    if not values["idempotency_key"]:
        values["idempotency_key"] = digest(
            ":".join(
                [
                    str(user.id),
                    operation,
                    host.id,
                    values["image_id"],
                    values["disk_key"],
                    values["hostname"],
                    report.id if report else "",
                ]
            )
        )
    elif len(values["idempotency_key"]) > 128:
        errors["form"] = "La solicitud no tiene un identificador válido."
    existing = db.scalar(select(Task).where(Task.idempotency_key == values["idempotency_key"]))
    if existing is not None:
        if existing.requested_by != user.id:
            errors["form"] = "La solicitud ya está siendo utilizada."
        else:
            return RedirectResponse(f"/tasks/{existing.id}", 303)
    if db.scalar(select(Task.id).where(Task.reservation_key == host.id)) is not None:
        errors["form"] = "Este equipo ya tiene una tarea activa."
    if errors or report is None or selected_disk is None or image is None or manifest is None:
        return render(
            request,
            "deployment.html",
            user=user,
            host=host,
            report=report,
            disks=disks,
            images=images,
            operation=operation,
            operation_label=TASK_OPERATION_LABELS[operation],
            values=values,
            errors=errors,
            status=422,
            section="hosts",
        )
    try:
        request.app.state.artifact_store.check_capacity()
    except StorageError as error:
        return render(
            request,
            "deployment.html",
            user=user,
            host=host,
            report=report,
            disks=disks,
            images=images,
            operation=operation,
            operation_label=TASK_OPERATION_LABELS[operation],
            values=values,
            errors={"form": str(error)},
            status=409,
            section="hosts",
        )
    task = Task(
        operation=operation,
        status="approved",
        requested_by=user.id,
        host_id=host.id,
        image_id=image.id,
        inventory_report_id=report.id,
        disk_selector=task_disk_selector(
            selected_disk,
            operation=operation,
            manifest=manifest,
            clone_hostname=values["hostname"],
        ),
        idempotency_key=values["idempotency_key"],
        reservation_key=host.id,
        phase="queued",
        bytes_processed=0,
        total_bytes=sum(artifact.size_bytes for artifact in manifest.artifacts),
        message=(
            "En espera de un agente PXE compatible para clonar y personalizar."
            if operation == "clone"
            else "En espera de un agente PXE compatible para restaurar el destino."
        ),
    )
    db.add(task)
    db.flush()
    add_event(
        db,
        task,
        event_type="created",
        phase="queued",
        total_bytes=task.total_bytes,
        message=task.message,
    )
    try:
        db.flush()
        record_audit(
            db,
            actor_user_id=user.id,
            action="task.create",
            resource_type="task",
            resource_id=task.id,
            detail=f"Tarea de {TASK_OPERATION_LABELS[operation].lower()} creada.",
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        record_audit(
            db,
            actor_user_id=user.id,
            action="task.create",
            resource_type="host",
            resource_id=host.id,
            outcome="failure",
            detail="No se pudo reservar el equipo para la operación.",
        )
        db.commit()
        return render(
            request,
            "deployment.html",
            user=user,
            host=host,
            report=report,
            disks=disks,
            images=images,
            operation=operation,
            operation_label=TASK_OPERATION_LABELS[operation],
            values=values,
            errors={"form": "No se pudo reservar el equipo; probá nuevamente."},
            status=409,
            section="hosts",
        )
    set_flash(
        request,
        f"{TASK_OPERATION_LABELS[operation]} encolada. Arrancá el equipo por PXE para iniciar.",
    )
    return RedirectResponse(f"/tasks/{task.id}", 303)


@router.get("/hosts/{host_id}/restore", response_class=HTMLResponse)
def restore_page(request: Request, host_id: UUID, db: Db) -> Response:
    return deployment_page(request, host_id, db, operation="restore")


@router.post("/hosts/{host_id}/restore")
async def restore_request(request: Request, host_id: UUID, db: Db) -> Response:
    return await deployment_request(request, host_id, db, operation="restore")


@router.get("/hosts/{host_id}/clone", response_class=HTMLResponse)
def clone_page(request: Request, host_id: UUID, db: Db) -> Response:
    return deployment_page(request, host_id, db, operation="clone")


@router.post("/hosts/{host_id}/clone")
async def clone_request(request: Request, host_id: UUID, db: Db) -> Response:
    return await deployment_request(request, host_id, db, operation="clone")


@router.get("/tasks", response_class=HTMLResponse)
def task_list(
    request: Request,
    db: Db,
    task_status: Annotated[
        str,
        Query(
            alias="status",
            pattern="^(all|approved|assigned|running|verifying|succeeded|failed|cancelled|intervention_required)$",
        ),
    ] = "all",
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
) -> Response:
    user = require_permission(request, db, "tasks.read")
    if has_permission(user, "tasks.cancel") and expire_stale_tasks(db):
        db.commit()
    query = select(Task)
    if task_status != "all":
        query = query.where(Task.status == task_status)
    matching = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, (matching + 19) // 20)
    page = min(page, pages)
    tasks = db.scalars(
        query.order_by(Task.created_at.desc()).offset((page - 1) * 20).limit(20)
    ).all()
    host_ids = {task.host_id for task in tasks}
    image_ids = {task.image_id for task in tasks}
    hosts = {
        host.id: host.name for host in db.scalars(select(Host).where(Host.id.in_(host_ids))).all()
    }
    images = {
        image.id: image.name
        for image in db.scalars(select(Image).where(Image.id.in_(image_ids))).all()
    }
    return render(
        request,
        "tasks.html",
        user=user,
        tasks=tasks,
        hosts=hosts,
        images=images,
        status_labels=TASK_LABELS,
        operation_labels=TASK_OPERATION_LABELS,
        task_status=task_status,
        matching=matching,
        page=page,
        pages=pages,
        section="tasks",
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "tasks.read", resource_type="task", resource_id=str(task_id)
    )
    if has_permission(user, "tasks.cancel") and expire_stale_tasks(db):
        db.commit()
    task = db.get(Task, str(task_id))
    if task is None:
        raise HTTPException(404, "No se encontró la tarea.")
    host = db.get(Host, task.host_id)
    image = db.get(Image, task.image_id)
    attempt = db.scalar(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task.id)
        .order_by(TaskAttempt.attempt_number.desc())
        .limit(1)
    )
    events = db.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task.id)
        .order_by(TaskEvent.created_at.desc())
        .limit(100)
    ).all()
    return render(
        request,
        "task_detail.html",
        user=user,
        task=task,
        host=host,
        image=image,
        attempt=attempt,
        events=events,
        status_label=TASK_LABELS.get(task.status, task.status),
        operation_label=TASK_OPERATION_LABELS.get(task.operation, task.operation),
        section="tasks",
    )


@router.get("/tasks/{task_id}/status")
def task_status(request: Request, task_id: UUID, db: Db) -> JSONResponse:
    user = require_permission(
        request, db, "tasks.read", resource_type="task", resource_id=str(task_id)
    )
    if has_permission(user, "tasks.cancel") and expire_stale_tasks(db):
        db.commit()
    task = db.get(Task, str(task_id))
    if task is None:
        raise HTTPException(404, "No se encontró la tarea.")
    return JSONResponse(task_status_payload(task), status_code=200)


@router.post("/tasks/{task_id}/cancel")
async def task_cancel(request: Request, task_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "tasks.cancel", resource_type="task", resource_id=str(task_id)
    )
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    if expire_stale_tasks(db):
        db.commit()
    task = db.get(Task, str(task_id))
    if task is None:
        raise HTTPException(404, "No se encontró la tarea.")
    try:
        changed = request_task_cancellation(
            db, task, reason=f"Cancelación solicitada por {user.username}."
        )
    except TaskError as error:
        raise HTTPException(409, str(error)) from None
    record_audit(
        db,
        actor_user_id=user.id,
        action="task.cancel",
        resource_type="task",
        resource_id=task.id,
        outcome="success" if changed else "failure",
        detail=(
            "Cancelación aplicada o solicitada al agente."
            if changed
            else "La tarea ya estaba terminada o cancelada."
        ),
    )
    db.commit()
    set_flash(
        request,
        "Tarea cancelada." if task.status == "cancelled" else "Cancelación solicitada al agente.",
    )
    if not changed:
        set_flash(request, "La tarea ya había terminado.")
    return RedirectResponse(f"/tasks/{task.id}", 303)


@router.post("/tasks/{task_id}/reconcile")
async def task_reconcile(request: Request, task_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "tasks.reconcile", resource_type="task", resource_id=str(task_id)
    )
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    if form.get("confirm") != "1":
        raise HTTPException(422, "Confirmá que el agente anterior está detenido.")
    if expire_stale_tasks(db):
        db.commit()
    task = db.get(Task, str(task_id))
    if task is None:
        raise HTTPException(404, "No se encontró la tarea.")
    try:
        reconcile_task(db, task)
    except TaskError as error:
        raise HTTPException(409, str(error)) from None
    if task.operation == "capture":
        image = db.get(Image, task.image_id)
        if image is not None:
            image.status = "capturing"
            image.failure_reason = ""
    try:
        request.app.state.artifact_store.remove_task_staging(task.id)
    except StorageError as error:
        db.rollback()
        raise HTTPException(409, str(error)) from None
    record_audit(
        db,
        actor_user_id=user.id,
        action="task.reconcile",
        resource_type="task",
        resource_id=task.id,
        detail="Operador confirmó que el agente anterior está detenido.",
    )
    db.commit()
    set_flash(
        request,
        "Tarea reconciliada. Se creó una oportunidad explícita para el nuevo intento.",
    )
    return RedirectResponse(f"/tasks/{task.id}", 303)


@router.get("/pairing", response_class=HTMLResponse)
def pairing_list(request: Request, db: Db) -> Response:
    user = require_permission(request, db, "pairing.read")
    current = now()
    if has_permission(user, "pairing.manage"):
        db.execute(
            update(PairingRequest)
            .where(
                PairingRequest.status.in_(["pending", "approved"]),
                PairingRequest.expires_at <= current,
            )
            .values(status="expired", challenge=None)
        )
        db.commit()
    requests = db.scalars(
        select(PairingRequest).order_by(PairingRequest.created_at.desc()).limit(50)
    ).all()
    hosts = db.scalars(select(Host).order_by(Host.name.asc())).all()
    return render(
        request,
        "pairing.html",
        user=user,
        requests=requests,
        hosts=hosts,
        section="pairing",
    )


@router.post("/pairing/{pairing_id}/approve")
async def pairing_approve(request: Request, pairing_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "pairing.manage", resource_type="pairing", resource_id=str(pairing_id)
    )
    pairing = get_pairing_request(db, pairing_id)
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    current = now()
    if pairing.status != "pending" or pairing.expires_at <= current:
        if pairing.status == "pending" and pairing.expires_at <= current:
            pairing.status, pairing.challenge = "expired", None
            db.commit()
        raise HTTPException(409, "La solicitud ya no está pendiente.")
    challenge = str(form.get("challenge", ""))
    if not challenge or not secrets.compare_digest(digest(challenge), pairing.challenge_hash):
        raise HTTPException(422, "El desafío no coincide con el que muestra el equipo.")
    selected_host = str(form.get("host_id", "")).strip()
    host: Host | None
    host_created = False
    if selected_host:
        try:
            host = get_host(db, UUID(selected_host))
        except ValueError:
            raise HTTPException(422, "El equipo seleccionado no es válido.") from None
        if host.mac_address != pairing.mac_address:
            raise HTTPException(422, "La MAC descubierta no coincide con el equipo seleccionado.")
    else:
        host = db.scalar(select(Host).where(Host.mac_address == pairing.mac_address))
        if host is None:
            host = Host(
                name=f"PXE {pairing.mac_address}",
                mac_address=pairing.mac_address,
                notes="Registrado desde una solicitud PXE aprobada.",
            )
            db.add(host)
            db.flush()
            host_created = True
    pairing.host_id = host.id
    pairing.status = "approved"
    pairing.approved_at = current
    pairing.challenge = None
    record_audit(
        db,
        actor_user_id=user.id,
        action="pairing.approve",
        resource_type="pairing",
        resource_id=pairing.id,
        detail="Solicitud PXE aprobada.",
    )
    if host_created:
        record_audit(
            db,
            actor_user_id=user.id,
            action="host.create",
            resource_type="host",
            resource_id=host.id,
            detail="Equipo creado desde una solicitud PXE aprobada.",
        )
    db.commit()
    set_flash(request, "Solicitud PXE aprobada. El equipo ya puede enviar su inventario.")
    return RedirectResponse("/pairing", 303)


@router.post("/pairing/{pairing_id}/reject")
async def pairing_reject(request: Request, pairing_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "pairing.manage", resource_type="pairing", resource_id=str(pairing_id)
    )
    pairing = get_pairing_request(db, pairing_id)
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    if pairing.status == "pending" and pairing.expires_at <= now():
        pairing.status, pairing.challenge = "expired", None
        db.commit()
    if pairing.status != "pending":
        raise HTTPException(409, "La solicitud ya no está pendiente.")
    reason = str(form.get("reason", "Solicitud rechazada por el administrador.")).strip()
    if len(reason) > 500:
        raise HTTPException(422, "El motivo no puede superar 500 caracteres.")
    pairing.status = "rejected"
    pairing.rejected_at = now()
    pairing.rejection_reason = reason or "Solicitud rechazada por el administrador."
    pairing.challenge = None
    record_audit(
        db,
        actor_user_id=user.id,
        action="pairing.reject",
        resource_type="pairing",
        resource_id=pairing.id,
        detail="Solicitud PXE rechazada.",
    )
    db.commit()
    set_flash(request, "Solicitud PXE rechazada.")
    return RedirectResponse("/pairing", 303)


@router.get("/hosts", response_class=HTMLResponse)
def host_list(
    request: Request,
    db: Db,
    q: Annotated[str, Query(max_length=200)] = "",
    state: Annotated[str, Query(pattern="^(all|ready|pending)$")] = "all",
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
) -> Response:
    user = require_permission(request, db, "hosts.read")
    query = select(Host)
    if q.strip():
        term = q.strip()
        query = query.where(
            or_(
                Host.name.icontains(term, autoescape=True),
                Host.mac_address.icontains(term.replace("-", ":"), autoescape=True),
                Host.hostname.icontains(term, autoescape=True),
            )
        )
    if state == "ready":
        query = query.where(Host.last_inventory_at.is_not(None))
    elif state == "pending":
        query = query.where(Host.last_inventory_at.is_(None))
    total = db.scalar(select(func.count()).select_from(Host)) or 0
    ready = (
        db.scalar(select(func.count()).select_from(Host).where(Host.last_inventory_at.is_not(None)))
        or 0
    )
    matching = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = min(page, max(1, (matching + 19) // 20))
    hosts = db.scalars(
        query.order_by(Host.created_at.desc()).offset((page - 1) * 20).limit(20)
    ).all()
    return render(
        request,
        "hosts.html",
        user=user,
        hosts=hosts,
        total=total,
        ready=ready,
        matching=matching,
        q=q,
        state=state,
        page=page,
        pages=max(1, (matching + 19) // 20),
    )


@router.get("/hosts/new", response_class=HTMLResponse)
def host_new(request: Request, db: Db) -> Response:
    return render(
        request,
        "host_form.html",
        user=require_permission(request, db, "hosts.create"),
        values={},
        errors={},
    )


@router.post("/hosts/new")
async def host_create(request: Request, db: Db) -> Response:
    user = require_permission(request, db, "hosts.create")
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    values = {key: str(form.get(key, "")) for key in ("name", "mac_address", "notes")}
    errors: dict[str, str] = {}
    try:
        data = HostInput.model_validate(values)
    except ValidationError as error:
        errors = {str(e["loc"][0]): e["msg"] for e in error.errors()}
    else:
        host = Host(**data.model_dump())
        db.add(host)
        try:
            db.flush()
            record_audit(
                db,
                actor_user_id=user.id,
                action="host.create",
                resource_type="host",
                resource_id=host.id,
                detail="Equipo registrado desde la web.",
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            errors["mac_address"] = "Ya existe un equipo registrado con esta MAC."
        else:
            set_flash(request, "Equipo registrado. Ya podés cargar su primer inventario.")
            return RedirectResponse(f"/hosts/{host.id}", 303)
    return render(request, "host_form.html", user=user, values=values, errors=errors, status=422)


@router.get("/hosts/{host_id}", response_class=HTMLResponse)
def host_detail(
    request: Request,
    host_id: UUID,
    db: Db,
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
) -> Response:
    user = require_permission(
        request, db, "hosts.read", resource_type="host", resource_id=str(host_id)
    )
    host = get_host(db, host_id)
    reports = select(InventoryReport).where(InventoryReport.host_id == host.id)
    count = db.scalar(select(func.count()).select_from(reports.subquery())) or 0
    page = min(page, max(1, (count + 9) // 10))
    history = db.scalars(
        reports.order_by(InventoryReport.received_at.desc()).offset((page - 1) * 10).limit(10)
    ).all()
    return render(
        request,
        "host_detail.html",
        user=user,
        host=host,
        report=latest_report(db, host),
        history=history,
        count=count,
        page=page,
        pages=max(1, (count + 9) // 10),
        token_active=bool(
            host.token_hash and host.token_expires_at and host.token_expires_at > now()
        ),
    )


@router.get("/hosts/{host_id}/edit", response_class=HTMLResponse)
def host_edit(request: Request, host_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "hosts.update", resource_type="host", resource_id=str(host_id)
    )
    host = get_host(db, host_id)
    return render(request, "host_form.html", user=user, host=host, values=host, errors={})


@router.post("/hosts/{host_id}/edit")
async def host_update(request: Request, host_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "hosts.update", resource_type="host", resource_id=str(host_id)
    )
    host = get_host(db, host_id)
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    values = {
        "name": str(form.get("name", "")),
        "notes": str(form.get("notes", "")),
        "mac_address": host.mac_address,
    }
    try:
        data = HostInput.model_validate(values)
    except ValidationError as error:
        errors = {str(e["loc"][0]): e["msg"] for e in error.errors()}
        return render(
            request,
            "host_form.html",
            user=user,
            host=host,
            values=values,
            errors=errors,
            status=422,
        )
    host.name, host.notes = data.name, data.notes
    record_audit(
        db,
        actor_user_id=user.id,
        action="host.update",
        resource_type="host",
        resource_id=host.id,
        detail="Datos administrativos del equipo actualizados.",
    )
    db.commit()
    set_flash(request, "Cambios guardados.")
    return RedirectResponse(f"/hosts/{host.id}", 303)


@router.get("/hosts/{host_id}/import", response_class=HTMLResponse)
def import_page(request: Request, host_id: UUID, db: Db) -> Response:
    return render(
        request,
        "import.html",
        user=require_permission(
            request, db, "inventory.import", resource_type="host", resource_id=str(host_id)
        ),
        host=get_host(db, host_id),
        error="",
    )


@router.post("/hosts/{host_id}/import")
async def import_report(request: Request, host_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "inventory.import", resource_type="host", resource_id=str(host_id)
    )
    host = get_host(db, host_id)
    async with request.form(max_files=1, max_fields=4) as form:
        verify_csrf(request, form.get("csrf"))
        upload = form.get("inventory")
        if not isinstance(upload, UploadFile):
            error_text = "Seleccioná el archivo JSON generado por el recolector."
        else:
            payload = await upload.read(request.app.state.settings.max_body_bytes + 1)
            try:
                inventory = Inventory.model_validate_json(payload)
                report, created = ingest_inventory(db, host, inventory, "web")
            except ValidationError as error:
                db.rollback()
                record_audit(
                    db,
                    actor_user_id=user.id,
                    action="inventory.import",
                    resource_type="host",
                    resource_id=host.id,
                    outcome="failure",
                    detail="El JSON de inventario no pasó la validación.",
                )
                db.commit()
                error_text = validation_message(error)
            except HTTPException as error:
                db.rollback()
                record_audit(
                    db,
                    actor_user_id=user.id,
                    action="inventory.import",
                    resource_type="host",
                    resource_id=host.id,
                    outcome="failure",
                    detail="El informe fue rechazado por las reglas de inventario.",
                )
                db.commit()
                error_text = str(error.detail)
            else:
                record_audit(
                    db,
                    actor_user_id=user.id,
                    action="inventory.import",
                    resource_type="host",
                    resource_id=host.id,
                    detail="Informe de inventario importado o repetido de forma idempotente.",
                )
                db.commit()
                message = "Inventario importado." if created else "El informe ya estaba registrado."
                set_flash(request, message)
                return RedirectResponse(f"/hosts/{host.id}/reports/{report.id}", 303)
    return render(request, "import.html", user=user, host=host, error=error_text, status=422)


@router.get("/hosts/{host_id}/reports/{report_id}", response_class=HTMLResponse)
def report_detail(request: Request, host_id: UUID, report_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "inventory.read", resource_type="report", resource_id=str(report_id)
    )
    host = get_host(db, host_id)
    report = db.get(InventoryReport, str(report_id))
    if report is None or report.host_id != host.id:
        raise HTTPException(404, "No se encontró el informe de este equipo.")
    return render(request, "report.html", user=user, host=host, report=report)


@router.post("/hosts/{host_id}/token")
async def inventory_token(request: Request, host_id: UUID, db: Db) -> Response:
    user = require_permission(
        request, db, "inventory.token", resource_type="host", resource_id=str(host_id)
    )
    host = get_host(db, host_id)
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    if form.get("action") == "revoke":
        revoked = revoke_credentials(db, host)
        record_audit(
            db,
            actor_user_id=user.id,
            action="token.revoke",
            resource_type="host",
            resource_id=host.id,
            detail=f"Credenciales de agente revocadas: {revoked}.",
        )
        db.commit()
        set_flash(request, "Credenciales de agente revocadas.")
        return RedirectResponse(f"/hosts/{host.id}", 303)
    if form.get("action") != "generate":
        raise HTTPException(400, "Acción inválida.")
    credential, token = issue_credential(db, host, request.app.state.settings)
    record_audit(
        db,
        actor_user_id=user.id,
        action="token.issue",
        resource_type="agent_credential",
        resource_id=credential.id,
        detail=(
            "Credencial de agente emitida; el valor no se registra y la anterior "
            "conserva gracia acotada."
        ),
    )
    db.commit()
    return render(request, "token.html", user=user, host=host, token=token)
