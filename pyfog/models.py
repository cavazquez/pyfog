import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pyfog.database import Base


def now() -> datetime:
    """Persist naive UTC consistently across SQLite and PostgreSQL."""
    return datetime.now(UTC).replace(tzinfo=None)


def identifier() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class LoginSession(Base):
    __tablename__ = "login_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime]


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    address_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(default=now, index=True)


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(100))
    mac_address: Mapped[str] = mapped_column(String(17), unique=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
    # Denormalized summary is updated in the same transaction as the report.
    hostname: Mapped[str] = mapped_column(String(253), default="")
    os_name: Mapped[str] = mapped_column(String(200), default="")
    last_inventory_at: Mapped[datetime | None]
    token_hash: Mapped[str | None] = mapped_column(String(64))
    token_expires_at: Mapped[datetime | None]


class InventoryReport(Base):
    __tablename__ = "inventory_reports"
    __table_args__ = (UniqueConstraint("host_id", "report_id", name="uq_host_report"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), index=True)
    report_id: Mapped[str] = mapped_column(String(36))
    collected_at: Mapped[datetime]
    received_at: Mapped[datetime] = mapped_column(default=now, index=True)
    source: Mapped[str] = mapped_column(String(10))
    fingerprint: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
