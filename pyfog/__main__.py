import argparse
import getpass
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pyfog.config import Settings
from pyfog.database import make_engine
from pyfog.models import LoginSession, User
from pyfog.rbac import ROLES
from pyfog.security import passwords

MAX_PASSWORD_LENGTH = 1024
MIN_PASSWORD_LENGTH = 12


def main() -> None:
    parser = argparse.ArgumentParser(description="Administración local de PyFog")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Aplicar migraciones pendientes")
    create_user = commands.add_parser("create-user", help="Crear una cuenta con un rol explícito")
    create_user.add_argument("--username", required=True)
    create_user.add_argument("--role", choices=ROLES, default="operator")
    for name in ("create-admin", "change-password"):
        subparser = commands.add_parser(name)
        subparser.add_argument("--username", required=True)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init-db":
        config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        config.attributes["database_url"] = settings.database_url
        command.upgrade(config, "head")
        print("Base de datos actualizada.")
        return
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", args.username):
        parser.error("El usuario debe contener letras, números, punto, guion o guion bajo.")
    engine = make_engine(settings.database_url)
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if args.command in ("create-admin", "create-user") and user:
            parser.error("El usuario ya existe. Usá change-password para recuperar acceso.")
        if args.command == "change-password" and not user:
            parser.error("El usuario no existe.")
        password = getpass.getpass(f"Contraseña (mínimo {MIN_PASSWORD_LENGTH} caracteres): ")
        confirmation = getpass.getpass("Repetir contraseña: ")
        if (
            len(password) < MIN_PASSWORD_LENGTH
            or len(password) > MAX_PASSWORD_LENGTH
            or password != confirmation
        ):
            parser.error(
                "Las contraseñas deben coincidir y tener entre "
                f"{MIN_PASSWORD_LENGTH} y {MAX_PASSWORD_LENGTH} caracteres."
            )
        if user:
            user.password_hash = passwords.hash(password)
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        else:
            db.add(
                User(
                    username=args.username,
                    password_hash=passwords.hash(password),
                    role="admin" if args.command == "create-admin" else args.role,
                )
            )
        db.commit()
    engine.dispose()
    if args.command == "create-user":
        print("Usuario creado.")
    elif args.command == "create-admin":
        print("Administrador creado.")
    else:
        print("Contraseña actualizada.")


if __name__ == "__main__":
    main()
