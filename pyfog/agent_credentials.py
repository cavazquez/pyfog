"""Issue, rotate and revoke credentials used by registered agent hosts."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from pyfog.config import Settings
from pyfog.models import AgentCredential, Host, now
from pyfog.security import digest


@dataclass(frozen=True)
class AgentAuthorization:
    """The host and credential authenticated by one bearer request."""

    host: Host
    credential: AgentCredential | None


def credential_is_usable(credential: AgentCredential, current: datetime | None = None) -> bool:
    current = current or now()
    return (
        credential.revoked_at is None
        and credential.expires_at > current
        and (credential.grace_until is None or credential.grace_until > current)
    )


def credential_for_token(
    db: Session, host_id: str, token: str, *, current: datetime | None = None
) -> AgentCredential | None:
    current = current or now()
    credential = db.scalar(
        select(AgentCredential).where(
            AgentCredential.host_id == host_id,
            AgentCredential.token_hash == digest(token),
        )
    )
    return credential if credential and credential_is_usable(credential, current) else None


def current_credential(
    db: Session, host_id: str, *, current: datetime | None = None
) -> AgentCredential | None:
    current = current or now()
    credentials = db.scalars(
        select(AgentCredential)
        .where(
            AgentCredential.host_id == host_id,
            AgentCredential.revoked_at.is_(None),
            AgentCredential.expires_at > current,
        )
        .order_by(AgentCredential.issued_at.desc(), AgentCredential.id.desc())
    ).all()
    return next((item for item in credentials if credential_is_usable(item, current)), None)


def _import_legacy_credential(db: Session, host: Host, current: datetime) -> None:
    """Keep direct ``create_all`` users and partially migrated MVP rows compatible."""

    if not host.token_hash or not host.token_expires_at:
        return
    existing = db.scalar(
        select(AgentCredential).where(AgentCredential.token_hash == host.token_hash)
    )
    if existing is None:
        db.add(
            AgentCredential(
                host_id=host.id,
                token_hash=host.token_hash,
                issued_at=current,
                expires_at=host.token_expires_at,
            )
        )


def issue_credential(db: Session, host: Host, settings: Settings) -> tuple[AgentCredential, str]:
    """Issue a new credential while keeping older credentials for bounded grace."""

    locked_host = db.scalar(select(Host).where(Host.id == host.id).with_for_update())
    if locked_host is None:
        msg = "No se encontró el equipo para emitir la credencial."
        raise ValueError(msg)
    host = locked_host
    current = now()
    _import_legacy_credential(db, host, current)
    grace_deadline = current + timedelta(seconds=settings.token_rotation_grace_seconds)
    active = db.scalars(
        select(AgentCredential).where(
            AgentCredential.host_id == host.id,
            AgentCredential.revoked_at.is_(None),
            AgentCredential.expires_at > current,
        )
    ).all()
    for credential in active:
        candidate = min(credential.expires_at, grace_deadline)
        if credential.grace_until is None or credential.grace_until > candidate:
            credential.grace_until = candidate
    token = secrets.token_urlsafe(32)
    credential = AgentCredential(
        host_id=host.id,
        token_hash=digest(token),
        issued_at=current,
        expires_at=current + timedelta(seconds=settings.token_seconds),
    )
    db.add(credential)
    host.token_hash = credential.token_hash
    host.token_expires_at = credential.expires_at
    db.flush()
    return credential, token


def revoke_credentials(db: Session, host: Host) -> int:
    """Revoke every generation, including a credential currently in grace."""

    current = now()
    credentials = db.scalars(
        select(AgentCredential).where(
            AgentCredential.host_id == host.id,
            AgentCredential.revoked_at.is_(None),
        )
    ).all()
    for credential in credentials:
        credential.revoked_at = current
    host.token_hash = None
    host.token_expires_at = None
    db.flush()
    return len(credentials)
