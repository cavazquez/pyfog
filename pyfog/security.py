import hashlib
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request
from pwdlib import PasswordHash
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from pyfog.audit import record_audit
from pyfog.models import LoginAttempt, LoginSession, User, now
from pyfog.rbac import has_permission, role_of

passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(32))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, provided: object) -> None:
    expected = request.session.get("csrf", "")
    if (
        not isinstance(provided, str)
        or not expected
        or not secrets.compare_digest(expected, provided)
    ):
        raise HTTPException(403, "La sesión del formulario venció. Recargá la página.")


def current_user(request: Request, db: Session) -> User | None:
    token = request.session.get("login")
    if not isinstance(token, str):
        return None
    session = db.get(LoginSession, digest(token))
    if session is None or session.expires_at <= now():
        return None
    return db.get(User, session.user_id)


def require_user(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if user is None:
        raise HTTPException(303, headers={"Location": "/login"})
    return user


def require_permission(
    request: Request,
    db: Session,
    permission: str,
    *,
    resource_type: str = "route",
    resource_id: str = "",
) -> User:
    """Require an authenticated user with one explicit permission.

    Denials are persisted before returning 403. The event contains only the
    route, role and permission name; it never receives form values or secrets.
    """

    user = require_user(request, db)
    if has_permission(user, permission):
        return user
    resolved_resource_id = (resource_id or request.url.path)[:100]
    reason = f"Rol {role_of(user) or 'desconocido'} sin permiso {permission}."
    record_audit(
        db,
        actor_user_id=user.id,
        action="access.denied",
        resource_type=resource_type,
        resource_id=resolved_resource_id,
        outcome="failure",
        decision="deny",
        reason=reason,
        detail="Acceso denegado por la política de permisos.",
    )
    db.commit()
    raise HTTPException(403, "No tenés permiso para realizar esta acción.")


def authenticate(request: Request, db: Session, username: str, password: str) -> User | None:
    cutoff = now() - timedelta(minutes=15)
    address = digest(request.client.host if request.client else "unknown")
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))
    attempts = (
        db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(LoginAttempt.address_hash == address)
        )
        or 0
    )
    if attempts >= 8:
        db.commit()
        raise HTTPException(429, "Demasiados intentos. Volvé a intentar en 15 minutos.")
    user = db.scalar(select(User).where(User.username == username))
    valid = passwords.verify(password, user.password_hash if user else DUMMY_HASH)
    if not valid or user is None:
        db.add(LoginAttempt(address_hash=address))
        db.commit()
        return None
    db.execute(delete(LoginAttempt).where(LoginAttempt.address_hash == address))
    db.execute(delete(LoginSession).where(LoginSession.expires_at < now()))
    # Invalidate any previous authenticated session in this browser.
    old_token = request.session.get("login")
    if isinstance(old_token, str):
        db.execute(delete(LoginSession).where(LoginSession.token_hash == digest(old_token)))
    raw_token = secrets.token_urlsafe(32)
    db.add(
        LoginSession(
            token_hash=digest(raw_token),
            user_id=user.id,
            expires_at=now() + timedelta(seconds=request.app.state.settings.session_seconds),
        )
    )
    db.commit()
    request.session.clear()
    request.session["login"] = raw_token
    csrf_token(request)
    return user
