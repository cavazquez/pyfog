import secrets
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pyfog.database import get_db
from pyfog.models import Host, InventoryReport, PairingRequest, now
from pyfog.schemas import Inventory, PairingRequestInput
from pyfog.security import digest
from pyfog.services import ingest_inventory

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


@router.get("/health", include_in_schema=False)
def health(db: Db) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.post("/api/v1/hosts/{host_id}/inventory", status_code=201)
def receive_inventory(
    request: Request, host_id: UUID, inventory: Inventory, db: Db
) -> JSONResponse:
    token = bearer_token(request)
    host = db.get(Host, str(host_id))
    expected = host.token_hash if host and host.token_hash else "0" * 64
    token_matches = secrets.compare_digest(digest(token), expected)
    if (
        not token
        or not token_matches
        or not host
        or not host.token_expires_at
        or host.token_expires_at <= now()
    ):
        raise HTTPException(
            401, "Credencial inválida o vencida.", headers={"WWW-Authenticate": "Bearer"}
        )
    report, created = ingest_inventory(db, host, inventory, "api")
    return JSONResponse(
        {"host_id": host.id, "report_id": report.report_id, "created": created},
        status_code=201 if created else 200,
    )


@router.post("/api/v1/pairing/requests", status_code=202)
def create_pairing_request(payload: PairingRequestInput, request: Request, db: Db) -> JSONResponse:
    """Create a short-lived discovery request without granting host permissions."""

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
