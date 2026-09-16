# PyFog

Registro e inventario de equipos Linux con **FastAPI**, SQLAlchemy y una interfaz web responsive.
Es la primera entrega de un proyecto de imágenes por red inspirado en FOG.

**Disponible:** alta y edición de equipos, identificación por MAC, recolector Linux, importación
JSON, API con tokens por equipo, inventario actual e historial. Acceso mediante administrador local.

**Planificado:** arranque PXE, captura de imágenes, restauración y clonación con Partclone.
El motor de imágenes todavía no está implementado.

[Roadmap y 47 issues atómicos](https://github.com/cavazquez/pyfog/issues/1) ·
[Hitos](https://github.com/cavazquez/pyfog/milestones) ·
[Arquitectura](docs/architecture.md) ·
[Contrato del MVP Linux](docs/adr/0001-mvp-linux.md)

## Iniciar en desarrollo

Requisitos: Python 3.12 a 3.14 y [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen
uv run python -m pyfog init-db
uv run python -m pyfog create-admin --username admin
uv run uvicorn pyfog.app:app --reload --host 127.0.0.1
```

Abrí **http://127.0.0.1:8000**. El comando de creación solicita una contraseña de al menos
12 caracteres; no hay credenciales predeterminadas. SQLite guarda los datos en `pyfog.db`,
excluido de Git. Las migraciones se aplican explícitamente, sin borrar datos al arrancar.

También están disponibles `make setup`, `make migrate` y `make run`.

## Registrar e inventariar un equipo

1. Ingresá a la web y elegí **Registrar equipo**.
2. Indicá nombre y MAC principal. En el equipo Linux podés consultar sus interfaces con `ip link`.
3. Copiá `scripts/collect_inventory.py` al equipo y ejecutá:

   ```bash
   python3 collect_inventory.py --output inventory.json
   ```

4. En la ficha del equipo, elegí **Importar inventario** y seleccioná ese JSON.

El recolector requiere Python 3.10 o superior y usa la biblioteca estándar. Lee `/proc`, `/sys`
y `/etc/os-release`; usa `lsblk` si está disponible. No necesita root para la información básica.
CPU, RAM, distribución, kernel, discos, interfaces y datos de fabricante se registran cuando están
disponibles; las lecturas no permitidas quedan como observaciones. No modifica discos ni red.
El archivo se crea con permisos `0600` y nunca se sobrescribe un archivo existente.

El informe debe contener la MAC principal del equipo. Una MAC ayuda a identificar una máquina;
**no es una credencial de autenticación**. La fecha del informe no representa conectividad actual.

### Envío directo al servidor

En la ficha, abrí **Envío de inventario con token** y generá una credencial. Vence a las 24 horas,
se muestra una sola vez y se guarda como hash. Podés rotarla o revocarla desde la misma pantalla.

En Bash, desde el equipo registrado:

```bash
read -rsp 'Token: ' PYFOG_INVENTORY_TOKEN; echo
export PYFOG_INVENTORY_TOKEN
python3 collect_inventory.py \
  --server https://pyfog.example \
  --host-id UUID_DEL_EQUIPO \
  --output inventory.json
unset PYFOG_INVENTORY_TOKEN
```

Reemplazá la URL y el UUID por los de tu instalación. Para la prueba local se admite
`--server http://127.0.0.1:8000`. En otro equipo se requiere HTTPS; una CA propia se configura con
`--ca-file ca.pem`. El recolector rechaza redirecciones y no usa proxies heredados del entorno.
El despliegue LAN con certificados y PXE pertenece a la siguiente etapa del roadmap.

Si falla el envío, el archivo generado queda disponible. Para reenviar exactamente el mismo informe,
con el token nuevamente en el entorno:

```bash
python3 collect_inventory.py --input inventory.json \
  --server https://pyfog.example --host-id UUID_DEL_EQUIPO
```

`POST /api/v1/hosts/{uuid}/inventory` requiere `Authorization: Bearer TOKEN` y JSON del esquema v1.
Responde `201` al crear, `200` al recibir de nuevo el mismo informe, `401` para credenciales inválidas,
`409` si se reutiliza `report_id` con otro contenido y `422` para un informe inválido.
El cuerpo de cualquier carga está limitado a 1 MiB, incluyendo el multipart de la web.

El [esquema de inventario](pyfog/schemas.py) valida tipos, fechas, límites y versión. Los informes
son inmutables y el más reciente por fecha de recolección alimenta la ficha; importar un informe
antiguo lo añade al historial sin reemplazar el hardware actual.

## Configuración

| Variable | Uso |
| --- | --- |
| `PYFOG_DATABASE_URL` | Base de datos; por defecto `sqlite:///./pyfog.db`. |
| `PYFOG_SECRET_KEY` | Firma de cookies. Usar un valor aleatorio de al menos 32 caracteres. |
| `PYFOG_ALLOWED_HOSTS` | Hosts permitidos, separados por comas. Por defecto solo loopback. |
| `PYFOG_ENV` | `production` exige clave explícita, cookies seguras y redirección HTTPS. |

Las variables se leen del entorno; no se carga automáticamente un archivo `.env`. En desarrollo,
si falta `PYFOG_SECRET_KEY`, se genera una clave efímera y reiniciar el servidor invalida las sesiones.
Las sesiones también tienen vencimiento del lado del servidor y se revocan al cerrar sesión.

Para recuperar el acceso:

```bash
uv run python -m pyfog change-password --username admin
```

Esto invalida las sesiones existentes de ese usuario. El servidor limita intentos de login, protege
los formularios con CSRF y escapa los datos recibidos al renderizar HTML. Los tokens de inventario
solo permiten actualizar el equipo para el que se emitieron.

## Calidad y dependencias

```bash
uv run ruff check .
uv run ruff format .          # aplicar formato
uv run ruff format --check .  # verificar sin modificar
uv run mypy
uv run pytest
uv run alembic check          # luego de aplicar migraciones
make audit                    # vulnerabilidades y secretos versionados
```

`make check` ejecuta lint, comprobación de formato, tipos y pruebas. Mypy está configurado en modo
estricto para aplicación y recolector. Ruff incluye reglas de errores, imports, modernización,
bugs, simplificaciones, comprehensions, nombres, seguridad, asincronía, pathlib, fechas, pytest,
builtins, retornos y uso de `print`; las excepciones están acotadas por archivo o línea.

`make audit` ejecuta `pip-audit` y el escaneo de secretos sobre los archivos versionados. La misma
comprobación bloquea pull requests y pushes; la política de excepciones y respuesta ante una
credencial expuesta está en [docs/security.md](docs/security.md).

Las dependencias directas tienen versiones exactas en `pyproject.toml`; `uv.lock` fija también las
transitivas. La CI instala con `uv sync --frozen` y verifica Python 3.12 y 3.14. Dependabot revisa
Python/uv y GitHub Actions **cada 1 de diciembre a las 09:00 de Buenos Aires**, abriendo PRs para
revisión. Esta programación usa el [soporte cron de Dependabot](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#schedule)
y no configura ni desactiva las actualizaciones de seguridad del repositorio.

## Alcance siguiente

El [roadmap](https://github.com/cavazquez/pyfog/issues/1) organiza cinco entregas:

1. Registro e inventario Linux.
2. Arranque PXE y registro desde el entorno de arranque.
3. Captura de imágenes.
4. Restauración y clonación.
5. Operación, documentación y validación completa del MVP.

Las imágenes tendrán una matriz inicial acotada a Linux x86_64, UEFI sin Secure Boot, GPT, raíz ext4,
ESP FAT32 y swap opcional; un disco por tarea y destino de igual o mayor capacidad. El inventario
actual puede describir hardware fuera de esa matriz. No se promete compatibilidad de formato con FOG.

Licencia del proyecto: [Apache-2.0](LICENSE).
