import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("PYFOG_DATABASE_URL", "sqlite:///./pyfog.db")
    )
    secret_key: str = field(default_factory=lambda: os.getenv("PYFOG_SECRET_KEY", ""))
    production: bool = field(default_factory=lambda: os.getenv("PYFOG_ENV") == "production")
    allowed_hosts: list[str] = field(
        default_factory=lambda: os.getenv("PYFOG_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(
            ","
        )
    )
    session_seconds: int = 3600
    token_seconds: int = 86400
    max_body_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if self.production and len(self.secret_key) < 32:
            raise ValueError("PYFOG_SECRET_KEY debe tener al menos 32 caracteres en producción.")
        if not self.secret_key:
            # A restart invalidates development sessions; no shared default secret.
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(48))


PACKAGE_DIR = Path(__file__).parent
