import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from pyfog.database import Base


def now() -> datetime:
    """Persist naive UTC consistently across SQLite and PostgreSQL."""
    return datetime.now(UTC).replace(tzinfo=None)


def identifier() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'operator', 'auditor')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(
        String(16), default="admin", server_default="admin", nullable=False
    )


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


class AgentCredential(Base):
    """Hashed bearer credential issued to one registered agent host."""

    __tablename__ = "agent_credentials"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_agent_credentials_token_hash"),
        Index("ix_agent_credentials_host_valid", "host_id", "revoked_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(default=now)
    expires_at: Mapped[datetime]
    grace_until: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


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


class PairingRequest(Base):
    """A short-lived, administrator-approved PXE discovery session."""

    __tablename__ = "pairing_requests"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_pairing_session"),
        Index("ix_pairing_status_expires", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    session_id: Mapped[str] = mapped_column(String(36))
    mac_address: Mapped[str] = mapped_column(String(17), index=True)
    challenge: Mapped[str | None] = mapped_column(String(128))
    challenge_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(default=now, index=True)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    host_id: Mapped[str | None] = mapped_column(
        ForeignKey("hosts.id", ondelete="SET NULL"), index=True
    )
    capability_hash: Mapped[str] = mapped_column(String(64))
    capability_used_at: Mapped[datetime | None]
    last_report_id: Mapped[str | None] = mapped_column(String(36))
    approved_at: Mapped[datetime | None]
    rejected_at: Mapped[datetime | None]
    rejection_reason: Mapped[str] = mapped_column(String(500), default="")


class Image(Base):
    """Catalog metadata for one immutable image identity and its publication state."""

    __tablename__ = "images"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'capturing', 'ready', 'failed')", name="ck_images_status"
        ),
        CheckConstraint("total_size_bytes IS NULL OR total_size_bytes >= 0", name="ck_images_size"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    manifest_image_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    source_host_id: Mapped[str | None] = mapped_column(
        ForeignKey("hosts.id", ondelete="SET NULL"), index=True
    )
    source_hostname: Mapped[str] = mapped_column(String(253), default="")
    captured_at: Mapped[datetime | None]
    total_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    compatibility: Mapped[str] = mapped_column(String(500), default="")
    integrity_verified_at: Mapped[datetime | None]
    failure_reason: Mapped[str] = mapped_column(String(500), default="")
    deleted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class AuditEvent(Base):
    """Secret-free, append-only operational record with web or host actor context."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("outcome IN ('success', 'failure')", name="ck_audit_outcome"),
        CheckConstraint("decision IN ('allow', 'deny')", name="ck_audit_decision"),
        Index("ix_audit_events_created", "created_at"),
        Index("ix_audit_events_resource", "resource_type", "resource_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_host_id: Mapped[str | None] = mapped_column(
        ForeignKey("hosts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(100), default="")
    outcome: Mapped[str] = mapped_column(String(16), default="success")
    decision: Mapped[str] = mapped_column(
        String(16), default="allow", server_default="allow", nullable=False
    )
    reason: Mapped[str] = mapped_column(String(500), default="", server_default="")
    detail: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(default=now)


class Task(Base):
    """Durable request for one long-running image operation."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("operation IN ('capture', 'restore', 'clone')", name="ck_tasks_operation"),
        CheckConstraint(
            "status IN ('draft', 'approved', 'assigned', 'running', 'verifying', "
            "'succeeded', 'failed', 'cancelled', 'intervention_required')",
            name="ck_tasks_status",
        ),
        CheckConstraint("bytes_processed >= 0", name="ck_tasks_bytes_processed"),
        CheckConstraint("total_bytes IS NULL OR total_bytes >= 0", name="ck_tasks_total_bytes"),
        Index("ix_tasks_status_created", "status", "created_at"),
        Index("ix_tasks_host_status", "host_id", "status"),
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        UniqueConstraint("reservation_key", name="uq_tasks_reservation_key"),
        UniqueConstraint("transfer_slot", name="uq_tasks_transfer_slot"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    operation: Mapped[str] = mapped_column(String(16), default="capture")
    status: Mapped[str] = mapped_column(String(32), default="approved", index=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="RESTRICT"), index=True)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    inventory_report_id: Mapped[str] = mapped_column(
        ForeignKey("inventory_reports.id", ondelete="RESTRICT")
    )
    disk_selector: Mapped[dict[str, Any]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    # These two columns make the single-host and single-transfer MVP limits enforceable by SQL.
    reservation_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
    transfer_slot: Mapped[int | None] = mapped_column(nullable=True)
    phase: Mapped[str] = mapped_column(String(32), default="queued")
    bytes_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    message: Mapped[str] = mapped_column(String(500), default="")
    failure_reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
    assigned_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    cancel_requested_at: Mapped[datetime | None]
    cancel_acknowledged_at: Mapped[datetime | None]


class TaskAttempt(Base):
    """One agent lease for a task; attempts are never silently reused."""

    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_task_attempt_number"),
        UniqueConstraint("agent_session_id", name="uq_task_agent_session"),
        Index("ix_task_attempt_lease", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(default=1)
    agent_session_id: Mapped[str] = mapped_column(String(36))
    agent_credential_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_credentials.id", ondelete="SET NULL"), nullable=True, index=True
    )
    capability_hash: Mapped[str] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime]
    last_heartbeat_at: Mapped[datetime] = mapped_column(default=now)
    last_sequence: Mapped[int] = mapped_column(default=0)
    phase: Mapped[str] = mapped_column(String(32), default="assigned")
    bytes_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(default=now)
    assigned_at: Mapped[datetime] = mapped_column(default=now)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    failure_reason: Mapped[str] = mapped_column(String(500), default="")


class TaskEvent(Base):
    """Bounded, secret-free progress history for a task and its attempts."""

    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint("attempt_id", "sequence", name="uq_task_event_sequence"),
        Index("ix_task_events_task_created", "task_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_attempts.id", ondelete="CASCADE"), nullable=True
    )
    sequence: Mapped[int | None]
    event_type: Mapped[str] = mapped_column(String(32))
    phase: Mapped[str] = mapped_column(String(32), default="queued")
    bytes_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    message: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(default=now, index=True)
