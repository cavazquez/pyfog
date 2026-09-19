import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DEVELOPMENT_ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
MAX_COORDINATOR_LEASE_SECONDS = 300
MIN_PRODUCTION_SECRET_LENGTH = 32


def environment_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    msg = f"{name} debe ser true o false."
    raise ValueError(msg)


def environment_list(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


def environment_int(name: str, *, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        msg = f"{name} debe ser un entero."
        raise ValueError(msg) from None


def session_secret() -> str:
    value = os.getenv("PYFOG_SECRET_KEY", "")
    secret_file = os.getenv("PYFOG_SECRET_KEY_FILE", "")
    if value and secret_file:
        msg = "Usá PYFOG_SECRET_KEY o PYFOG_SECRET_KEY_FILE, no ambos."
        raise ValueError(msg)
    if not secret_file:
        return value
    try:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError as error:
        msg = "No se pudo leer PYFOG_SECRET_KEY_FILE."
        raise ValueError(msg) from error


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("PYFOG_DATABASE_URL", "sqlite:///./pyfog.db")
    )
    secret_key: str = field(default_factory=session_secret)
    production: bool = field(default_factory=lambda: os.getenv("PYFOG_ENV") == "production")
    debug: bool = field(default_factory=lambda: environment_flag("PYFOG_DEBUG"))
    allowed_hosts: list[str] = field(
        default_factory=lambda: environment_list("PYFOG_ALLOWED_HOSTS")
    )
    trusted_proxy_ips: list[str] = field(
        default_factory=lambda: environment_list("PYFOG_TRUSTED_PROXY_IPS")
    )
    session_seconds: int = 3600
    token_seconds: int = 86400
    token_rotation_grace_seconds: int = 300
    pairing_seconds: int = 900
    max_body_bytes: int = 1_048_576
    image_store_path: Path = field(
        default_factory=lambda: Path(os.getenv("PYFOG_IMAGE_STORE", "./pyfog-images"))
    )
    max_image_bytes: int = 2**50
    max_chunk_bytes: int = 512 * 1024
    task_lease_seconds: int = 90
    task_heartbeat_seconds: int = 20
    min_storage_free_bytes: int = 64 * 1024 * 1024
    coordinator_id: str = field(default_factory=lambda: os.getenv("PYFOG_COORDINATOR_ID", ""))
    coordinator_lease_seconds: int = field(
        default_factory=lambda: environment_int("PYFOG_COORDINATOR_LEASE_SECONDS", default=15)
    )

    def _validate_production_settings(
        self, allowed_hosts: list[str], trusted_proxy_ips: list[str]
    ) -> None:
        if self.production and len(self.secret_key) < MIN_PRODUCTION_SECRET_LENGTH:
            msg = "PYFOG_SECRET_KEY debe tener al menos 32 caracteres en producción."
            raise ValueError(msg)
        if not allowed_hosts:
            if self.production:
                msg = "PYFOG_ALLOWED_HOSTS debe configurarse en producción."
                raise ValueError(msg)
            allowed_hosts.extend(DEVELOPMENT_ALLOWED_HOSTS)
        if self.production and self.debug:
            msg = "PYFOG_DEBUG no puede activarse en producción."
            raise ValueError(msg)
        if self.production and "*" in allowed_hosts:
            msg = "PYFOG_ALLOWED_HOSTS no puede incluir * en producción."
            raise ValueError(msg)
        if self.production and not trusted_proxy_ips:
            msg = "PYFOG_TRUSTED_PROXY_IPS debe configurarse en producción."
            raise ValueError(msg)
        if self.production and "*" in trusted_proxy_ips:
            msg = "PYFOG_TRUSTED_PROXY_IPS no puede incluir * en producción."
            raise ValueError(msg)

    def _validate_limits(self) -> None:
        if self.max_body_bytes <= 0:
            msg = "max_body_bytes debe ser positivo."
            raise ValueError(msg)
        if self.token_seconds <= 0:
            msg = "token_seconds debe ser positivo."
            raise ValueError(msg)
        if self.token_rotation_grace_seconds <= 0:
            msg = "token_rotation_grace_seconds debe ser positivo."
            raise ValueError(msg)
        if not 0 < self.max_chunk_bytes <= self.max_body_bytes:
            msg = "max_chunk_bytes debe ser positivo y caber en max_body_bytes."
            raise ValueError(msg)
        if self.max_image_bytes <= 0:
            msg = "max_image_bytes debe ser positivo."
            raise ValueError(msg)
        if self.task_lease_seconds <= 0 or self.task_heartbeat_seconds <= 0:
            msg = "Los tiempos de tareas deben ser positivos."
            raise ValueError(msg)
        if (
            self.coordinator_lease_seconds <= 0
            or self.coordinator_lease_seconds > MAX_COORDINATOR_LEASE_SECONDS
        ):
            msg = "coordinator_lease_seconds debe estar entre 1 y 300 segundos."
            raise ValueError(msg)

    def __post_init__(self) -> None:
        allowed_hosts = [host.strip() for host in self.allowed_hosts if host.strip()]
        trusted_proxy_ips = [host.strip() for host in self.trusted_proxy_ips if host.strip()]
        self._validate_production_settings(allowed_hosts, trusted_proxy_ips)
        self._validate_limits()
        if self.min_storage_free_bytes < 0:
            msg = "min_storage_free_bytes no puede ser negativo."
            raise ValueError(msg)
        if not self.secret_key:
            # A restart invalidates development sessions; no shared default secret.
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(48))
        object.__setattr__(self, "allowed_hosts", allowed_hosts)
        object.__setattr__(self, "trusted_proxy_ips", trusted_proxy_ips)


PACKAGE_DIR = Path(__file__).parent
