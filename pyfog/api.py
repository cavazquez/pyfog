import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from pyfog.database import get_db
from pyfog.models import Host, now
from pyfog.schemas import Inventory
from pyfog.security import digest
from pyfog.services import ingest_inventory

router = APIRouter()
Db = Annotated[Session, Depends(get_db)]


@router.get("/health", include_in_schema=False)
def health(db: Db) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.post("/api/v1/hosts/{host_id}/inventory", status_code=201)
def receive_inventory(
    request: Request, host_id: UUID, inventory: Inventory, db: Db
) -> JSONResponse:
    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
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
