# PyFog

Registro e inventario de equipos Linux con **FastAPI**, SQLAlchemy y una interfaz web responsive.
Es la primera entrega de un proyecto de imágenes por red inspirado en FOG.

**Disponible:** alta y edición de equipos, identificación por MAC, descubrimiento PXE con aprobación
administrativa, recolector Linux, importación JSON, API con tokens por equipo, inventario actual e
historial, catálogo web de fichas de imágenes, captura, restauración y clonación Linux con tareas
persistentes, leases, progreso, cancelación cooperativa, reconciliación, auditoría, healthchecks,
backup y almacenamiento por fragmentos con publicación verificada. Acceso mediante administrador
local.

**Alcance restante:** la operación multi-coordinador y la matriz completa de pruebas E2E siguen
fuera del MVP actual. El modo `imaging` requiere un initramfs específico y una provisión controlada
del token del equipo.

[Roadmap y 47 issues atómicos](https://github.com/cavazquez/pyfog/issues/1) ·
[Hitos](https://github.com/cavazquez/pyfog/milestones) ·
[Arquitectura](docs/architecture.md) ·
[Contrato del MVP Linux](docs/adr/0001-mvp-linux.md) ·
[Manifiesto de imágenes](docs/image-manifest.md) ·
[Backup y recuperación](docs/backup-recovery.md) ·
[Guía operativa en español](docs/guia-es.md) ·
[Accesibilidad](docs/accessibility.md) ·
[Despliegue LAN](deploy/README.md) ·
[Release v0.1.0](docs/release-0.1.0.md) ·
[Procedencia](docs/provenance.md)

## Iniciar en desarrollo

Para una experiencia reproducible con Docker/Compose, consultá [Despliegues reproducibles](deploy/README.md)
y ejecutá `make dev-up`, `make dev-migrate` y `make dev-admin`. Los comandos locales de abajo siguen
siendo útiles para iterar sin contenedores.

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
El [despliegue LAN con certificados](docs/https.md) ya está documentado. El [agente de arranque](agent/README.md) se construye con hashes
reproducibles y tiene `inventory` como modo predeterminado; nunca monta discos ni escribe bloques.

### Descubrimiento desde PXE

El perfil [PXE/UEFI](pxe/README.md) pasa la URL HTTPS del servidor y activa `pyfog.pair=1`. Al
arrancar, el agente muestra un desafío efímero en la consola y publica una solicitud en **Equipos
descubiertos**. El administrador compara el desafío, la asocia a un equipo nuevo o existente y la
aprueba. El agente recibe una capacidad temporal, envía un único inventario y la capacidad deja de
servir para cualquier otra operación. Una solicitud rechazada o vencida no habilita tareas.

Para probar el mismo flujo desde un Linux con red, ejecutá `python3 collect_inventory.py --pair
--server https://pyfog.example --ca-file ca.pem`. El comando conserva el informe en memoria durante
la espera; `--pair-timeout` permite ajustar el tiempo máximo de aprobación.

El flujo usa `POST /api/v1/pairing/requests` para crear la solicitud, `GET
/api/v1/pairing/requests/{id}` para consultar su estado y `POST
/api/v1/pairing/requests/{id}/inventory` para entregar el primer informe. Las dos últimas rutas
requieren la capacidad Bearer efímera que devuelve la primera; después del informe, esa capacidad
no permite otro `report_id`.

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

### Catálogo de imágenes

En **Imágenes** podés crear y editar la ficha de una versión Linux con un nombre único y una
descripción. Cada ficha conserva su UUID aunque cambie el nombre. La lista admite búsqueda,
filtro por estado y paginación, y muestra origen, captura, tamaño y compatibilidad cuando el
manifiesto los informa. Una ficha nueva queda en `Borrador`. Al encolar una captura pasa a
`Capturando`; el agente la deja en `Lista` sólo después de transferir, validar y publicar
atómicamente todos sus artefactos. Una falla queda en `Fallida` y requiere una nueva captura. Sólo
una imagen `Lista` con manifiesto y verificación de integridad puede seleccionarse para restaurar.
Crear una ficha con el mismo nombre exacto devuelve un error y no reemplaza una imagen existente.

## Configuración

| Variable | Uso |
| --- | --- |
| `PYFOG_DATABASE_URL` | Base de datos; por defecto `sqlite:///./pyfog.db`. |
| `PYFOG_SECRET_KEY` | Firma de cookies. Usar un valor aleatorio de al menos 32 caracteres. |
| `PYFOG_SECRET_KEY_FILE` | Alternativa para leer la clave de sesión desde un archivo secreto; no se combina con la variable anterior. |
| `PYFOG_ALLOWED_HOSTS` | Hosts permitidos, separados por comas. En desarrollo usa loopback; en producción es obligatorio y no admite `*`. |
| `PYFOG_TRUSTED_PROXY_IPS` | IPs o CIDRs del proxy que puede enviar `X-Forwarded-*`; obligatorio en producción y no admite `*`. |
| `PYFOG_DEBUG` | `true` o `false`; producción rechaza `true`. |
| `PYFOG_ENV` | `production` exige clave, hosts y proxy explícitos, cookies seguras y redirección HTTPS. |
| `PYFOG_IMAGE_STORE` | Directorio de staging y publicaciones de imágenes; por defecto `./pyfog-images`. |

Las variables se leen del entorno; no se carga automáticamente un archivo `.env`. En desarrollo,
si falta `PYFOG_SECRET_KEY`, se genera una clave efímera y reiniciar el servidor invalida las sesiones.
Las sesiones también tienen vencimiento del lado del servidor y se revocan al cerrar sesión. La guía
de [HTTPS, proxy y CA local](docs/https.md) incluye el despliegue Docker con Caddy, certificados
propios y el material público que recibirá el agente de arranque.

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
./scripts/check.sh             # check completo del proyecto
```

`./scripts/check.sh` ejecuta el lint, la comprobación de formato, tipos, compilación, pruebas,
validación de scripts shell, manifiestos, migraciones y auditorías. `make check` es un alias para
ese script. Mypy está configurado en modo estricto para aplicación y recolector. Ruff incluye reglas de errores, imports, modernización,
bugs, simplificaciones, comprehensions, nombres, seguridad, asincronía, pathlib, fechas, pytest,
builtins, retornos y uso de `print`; las excepciones están acotadas por archivo o línea.

`make release-check` comprueba la versión común del servidor y agente, la documentación de entrega
y, si se agrega `--require-artifacts`, los manifiestos y checksums de `dist/agent` y `dist/pxe`.
`make e2e-plan` muestra la matriz reproducible; `make e2e` requiere un host Linux con QEMU/OVMF.

`make audit` ejecuta `pip-audit` y el escaneo de secretos sobre los archivos versionados. La misma
comprobación bloquea pull requests y pushes; la política de excepciones y respuesta ante una
credencial expuesta está en [docs/security.md](docs/security.md).

Las dependencias directas tienen versiones exactas en `pyproject.toml`; `uv.lock` fija también las
transitivas. La CI instala con `uv sync --frozen` y verifica Python 3.12 y 3.14. Dependabot revisa
Python/uv y GitHub Actions **cada 1 de diciembre a las 09:00 de Buenos Aires**, abriendo PRs para
revisión. Esta programación usa el [soporte cron de Dependabot](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#schedule)
y no configura ni desactiva las actualizaciones de seguridad del repositorio.

## Estado de la entrega

El [roadmap](https://github.com/cavazquez/pyfog/issues/1) organizó cinco entregas:

1. Registro e inventario Linux.
2. Arranque PXE y registro desde el entorno de arranque.
3. Captura de imágenes Linux con validación y publicación verificadas (completa).
4. Restauración y clonación (completa).
5. Operación, documentación y validación del MVP (completa; la ejecución UEFI requiere el host del laboratorio).

Las imágenes tendrán una matriz inicial acotada a Linux x86_64, UEFI sin Secure Boot, GPT, raíz ext4,
ESP FAT32 y swap opcional; un disco por tarea y destino de igual o mayor capacidad. El inventario
actual puede describir hardware fuera de esa matriz. No se promete compatibilidad de formato con FOG.

## Laboratorio y E2E UEFI

El laboratorio reproducible [lab/README.md](lab/README.md) crea dos VMs QEMU/OVMF con discos
descartables y una red aislada en loopback. `./lab/e2e.sh run` combina el contrato del coordinador,
los casos negativos y el smoke de arranque UEFI sin exponer discos físicos ni la red de una LAN.

## Construir el agente de arranque

```bash
agent/build-agent doctor --mode inventory-only --kernel /boot/vmlinuz-$(uname -r)
agent/build-agent build --mode inventory-only --output-dir dist/agent
agent/build-agent verify --output-dir dist/agent
```

El modo `imaging` añade `sgdisk`, `partclone.ext4` y `partclone.fat` al initramfs y falla si alguno
no está instalado. Antes de publicar los archivos en PXE hay que revisar `manifest.json` y
`SHA256SUMS`; el servidor no descarga paquetes durante el arranque.

Para ejecutar una captura, primero encolala desde **Capturar una imagen** en la ficha de un equipo
con inventario actualizado. Después arrancá ese equipo con un agente construido en modo `imaging` y
estos parámetros, ajustando la URL, el UUID y el certificado de tu instalación:

```text
pyfog.mode=imaging pyfog.net=dhcp pyfog.server=https://pyfog.example
pyfog.host_id=UUID_DEL_EQUIPO pyfog.token_file=/run/pyfog/token
pyfog.ca_file=/etc/pyfog/ca.pem
```

El archivo indicado por `pyfog.token_file` debe ser creado en el entorno de arranque por un
proceso controlado y contener el token vigente de ese equipo con permisos `0600`. El token no debe
viajar en la línea de comandos, DHCP, iPXE ni una URL. El detalle de la tarea muestra el lease, el
progreso y los eventos; si el agente se detiene, la tarea queda para intervención y no se reasigna
automáticamente.

El perfil [PXE/UEFI](pxe/README.md) genera el menú iPXE con inventario y retorno al disco local;
el arranque `imaging` se agrega mediante un perfil controlado que pueda provisionar el token.
Usa el DHCP existente, exige una URL HTTPS sin credenciales y separa la raíz TFTP de la publicación
HTTPS. Si el servidor o la descarga fallan, el perfil abandona una sola vez hacia el firmware o el
disco local.

Licencia del proyecto: [Apache-2.0](LICENSE).
