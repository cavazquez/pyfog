# Despliegues reproducibles

PyFog se distribuye como una imagen de aplicación construida desde `deploy/Dockerfile` y un
Compose de SQLite. La imagen fija Python 3.12.13 y uv; Caddy está fijado en `2.11.4-alpine`.
El proceso `web` contiene también el coordinador de leases del MVP: no hay un segundo escritor ni
una base compartida entre contenedores.

## Desarrollo

Requisitos: Docker Engine con Compose v2, 2 GB libres y puertos locales disponibles. El perfil
predeterminado no arranca DHCP, TFTP, PXE ni monta discos del host; sólo expone FastAPI en
`127.0.0.1:8000` y guarda SQLite/artefactos en el volumen nombrado `pyfog_dev_data`.

```bash
make dev-up
make dev-migrate
make dev-admin PYFOG_ADMIN_USERNAME=admin
```

`make dev-up` monta el código Python, templates, scripts y migraciones para que `uvicorn --reload`
refleje cambios. `make dev-migrate` aplica migraciones explícitamente y puede repetirse. Para
detener sin borrar datos:

```bash
make dev-down
```

Para borrar únicamente los datos del proyecto de desarrollo, de forma deliberada:

```bash
make dev-clean
```

## LAN

Antes de arrancar, creá el secreto de sesión fuera del repo y definí `PYFOG_PUBLIC_HOST`:

```bash
cd deploy
install -d -m 700 secrets
openssl rand -base64 48 > secrets/session-key
chmod 600 secrets/session-key
export PYFOG_PUBLIC_HOST=pyfog.lab.test
export PYFOG_SESSION_KEY_FILE="$PWD/secrets/session-key"
docker compose -f compose.local-ca.yaml --profile admin run --rm migrate
docker compose -f compose.local-ca.yaml --profile admin run --rm migrate \
  python -m pyfog create-admin --username admin
docker compose -f compose.local-ca.yaml up -d
```

La base y el almacén viven en `pyfog_data`; los datos de Caddy viven en `caddy_data` y
`caddy_config`. Un reinicio o una actualización conserva esos volúmenes. Para certificado propio,
agregá `compose.custom-certificate.yaml` y definí la ruta de la cadena y de la clave privada en el
entorno. Los puertos HTTPS/HTTP se configuran con `PYFOG_HTTPS_PORT` y `PYFOG_HTTP_PORT`; el host
público y la lista de proxies confiables deben seguir siendo explícitos.

## PXE explícito

El perfil LAN normal no inicia DHCP ni proxyDHCP. Construí y verificá una publicación PXE con
`pxe/build-pxe`, y luego iniciá el componente estático sólo cuando hayas elegido el DHCP existente
de la red:

```bash
export PYFOG_PXE_DIR="$PWD/../dist/pxe"
docker compose \
  -f compose.local-ca.yaml \
  -f compose.pxe.yaml \
  --profile pxe up -d
```

El servicio `pxe` es un Caddy de solo lectura que sirve únicamente `dist/pxe/http` por el path
HTTPS `/boot`; no tiene acceso a la base, al almacén de imágenes ni a discos del host. DHCP/TFTP
deben ser provistos por una infraestructura elegida explícitamente y apuntar al bootstrap revisado.
No se incorpora un servicio DHCP dentro de Compose para evitar anunciarlo accidentalmente en una
LAN.

## Salud, migraciones y rollback

`/health/live` sólo confirma que el proceso responde; `/health/ready` comprueba DB, almacenamiento
y el coordinador integrado. Caddy espera el readiness del servicio web antes de arrancar.

Para actualizar, respaldá primero, construí la nueva imagen, aplicá las migraciones y reiniciá:

```bash
uv run python -m scripts.backup_server backup --output /var/backups/pyfog-before-update
docker compose -f compose.local-ca.yaml --profile admin run --rm migrate
docker compose -f compose.local-ca.yaml up -d --build
```

Para volver atrás, detené la publicación PXE si está activa, seleccioná el commit/tag anterior,
reconstruí la imagen y arrancá con los mismos volúmenes. No ejecutes `down -v` en una instalación
LAN: elimina datos. Si una migración no es compatible con la versión anterior, recuperá el backup
en una instalación vacía siguiendo [backup y recuperación](../docs/backup-recovery.md), verificá el
catálogo y habilitá el servicio recién después de reconciliar las tareas marcadas.
