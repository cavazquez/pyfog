import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DEVELOPMENT_ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]


def environment_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} debe ser true o false.")


def environment_list(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


def session_secret() -> str:
    value = os.getenv("PYFOG_SECRET_KEY", "")
    secret_file = os.getenv("PYFOG_SECRET_KEY_FILE", "")
    if value and secret_file:
        raise ValueError("Usá PYFOG_SECRET_KEY o PYFOG_SECRET_KEY_FILE, no ambos.")
    if not secret_file:
        return value
    try:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError("No se pudo leer PYFOG_SECRET_KEY_FILE.") from error


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

    def __post_init__(self) -> None:
        allowed_hosts = [host.strip() for host in self.allowed_hosts if host.strip()]
        trusted_proxy_ips = [host.strip() for host in self.trusted_proxy_ips if host.strip()]
        if self.production and len(self.secret_key) < 32:
            raise ValueError("PYFOG_SECRET_KEY debe tener al menos 32 caracteres en producción.")
        if not allowed_hosts:
            if self.production:
                raise ValueError("PYFOG_ALLOWED_HOSTS debe configurarse en producción.")
            allowed_hosts = DEVELOPMENT_ALLOWED_HOSTS.copy()
        if self.production and self.debug:
            raise ValueError("PYFOG_DEBUG no puede activarse en producción.")
        if self.production and "*" in allowed_hosts:
            raise ValueError("PYFOG_ALLOWED_HOSTS no puede incluir * en producción.")
        if self.production and not trusted_proxy_ips:
            raise ValueError("PYFOG_TRUSTED_PROXY_IPS debe configurarse en producción.")
        if self.production and "*" in trusted_proxy_ips:
            raise ValueError("PYFOG_TRUSTED_PROXY_IPS no puede incluir * en producción.")
        if self.max_body_bytes <= 0:
            raise ValueError("max_body_bytes debe ser positivo.")
        if self.token_seconds <= 0:
            raise ValueError("token_seconds debe ser positivo.")
        if self.token_rotation_grace_seconds <= 0:
            raise ValueError("token_rotation_grace_seconds debe ser positivo.")
        if not 0 < self.max_chunk_bytes <= self.max_body_bytes:
            raise ValueError("max_chunk_bytes debe ser positivo y caber en max_body_bytes.")
        if self.max_image_bytes <= 0:
            raise ValueError("max_image_bytes debe ser positivo.")
        if self.task_lease_seconds <= 0 or self.task_heartbeat_seconds <= 0:
            raise ValueError("Los tiempos de tareas deben ser positivos.")
        if self.min_storage_free_bytes < 0:
            raise ValueError("min_storage_free_bytes no puede ser negativo.")
        if not self.secret_key:
            # A restart invalidates development sessions; no shared default secret.
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(48))
        object.__setattr__(self, "allowed_hosts", allowed_hosts)
        object.__setattr__(self, "trusted_proxy_ips", trusted_proxy_ips)


PACKAGE_DIR = Path(__file__).parent
