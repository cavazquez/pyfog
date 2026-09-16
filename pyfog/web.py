import secrets
from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from pyfog.config import PACKAGE_DIR
from pyfog.database import get_db
from pyfog.models import Host, InventoryReport, LoginSession, now
from pyfog.schemas import HostInput, Inventory
from pyfog.security import authenticate, csrf_token, digest, require_user, verify_csrf
from pyfog.services import ingest_inventory

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
Db = Annotated[Session, Depends(get_db)]


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


def render(request: Request, template: str, *, status: int = 200, **context: Any) -> Response:
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"csrf": csrf_token(request), **context},
        status_code=status,
    )


def get_host(db: Session, host_id: UUID) -> Host:
    host = db.get(Host, str(host_id))
    if host is None:
        raise HTTPException(404, "No se encontró el equipo.")
    return host


def latest_report(db: Session, host: Host) -> InventoryReport | None:
    return db.scalar(
        select(InventoryReport)
        .where(InventoryReport.host_id == host.id)
        .order_by(InventoryReport.collected_at.desc(), InventoryReport.received_at.desc())
        .limit(1)
    )


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
    return RedirectResponse("/login", 303)


@router.get("/")
def home() -> Response:
    return RedirectResponse("/hosts", 303)


@router.get("/hosts", response_class=HTMLResponse)
def host_list(
    request: Request,
    db: Db,
    q: Annotated[str, Query(max_length=200)] = "",
    state: Annotated[str, Query(pattern="^(all|ready|pending)$")] = "all",
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
) -> Response:
    user = require_user(request, db)
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
    return render(request, "host_form.html", user=require_user(request, db), values={}, errors={})


@router.post("/hosts/new")
async def host_create(request: Request, db: Db) -> Response:
    user = require_user(request, db)
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
            db.commit()
        except IntegrityError:
            db.rollback()
            errors["mac_address"] = "Ya existe un equipo registrado con esta MAC."
        else:
            return RedirectResponse(f"/hosts/{host.id}", 303)
    return render(request, "host_form.html", user=user, values=values, errors=errors, status=422)


@router.get("/hosts/{host_id}", response_class=HTMLResponse)
def host_detail(
    request: Request,
    host_id: UUID,
    db: Db,
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
) -> Response:
    user = require_user(request, db)
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
    user = require_user(request, db)
    host = get_host(db, host_id)
    return render(request, "host_form.html", user=user, host=host, values=host, errors={})


@router.post("/hosts/{host_id}/edit")
async def host_update(request: Request, host_id: UUID, db: Db) -> Response:
    user = require_user(request, db)
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
    db.commit()
    return RedirectResponse(f"/hosts/{host.id}", 303)


@router.get("/hosts/{host_id}/import", response_class=HTMLResponse)
def import_page(request: Request, host_id: UUID, db: Db) -> Response:
    return render(
        request,
        "import.html",
        user=require_user(request, db),
        host=get_host(db, host_id),
        error="",
    )


@router.post("/hosts/{host_id}/import")
async def import_report(request: Request, host_id: UUID, db: Db) -> Response:
    user = require_user(request, db)
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
                report, _ = ingest_inventory(db, host, inventory, "web")
            except ValidationError as error:
                error_text = validation_message(error)
            except HTTPException as error:
                error_text = str(error.detail)
            else:
                return RedirectResponse(f"/hosts/{host.id}/reports/{report.id}", 303)
    return render(request, "import.html", user=user, host=host, error=error_text, status=422)


@router.get("/hosts/{host_id}/reports/{report_id}", response_class=HTMLResponse)
def report_detail(request: Request, host_id: UUID, report_id: UUID, db: Db) -> Response:
    user = require_user(request, db)
    host = get_host(db, host_id)
    report = db.get(InventoryReport, str(report_id))
    if report is None or report.host_id != host.id:
        raise HTTPException(404, "No se encontró el informe de este equipo.")
    return render(request, "report.html", user=user, host=host, report=report)


@router.post("/hosts/{host_id}/token")
async def inventory_token(request: Request, host_id: UUID, db: Db) -> Response:
    user = require_user(request, db)
    host = get_host(db, host_id)
    form = await request.form()
    verify_csrf(request, form.get("csrf"))
    if form.get("action") == "revoke":
        host.token_hash, host.token_expires_at = None, None
        db.commit()
        return RedirectResponse(f"/hosts/{host.id}", 303)
    if form.get("action") != "generate":
        raise HTTPException(400, "Acción inválida.")
    token = secrets.token_urlsafe(32)
    host.token_hash = digest(token)
    host.token_expires_at = now() + timedelta(seconds=request.app.state.settings.token_seconds)
    db.commit()
    return render(request, "token.html", user=user, host=host, token=token)
