# Despliegues reproducibles

PyFog se distribuye como una imagen de aplicación construida desde `deploy/Dockerfile` y un
Compose de SQLite. La imagen fija Python 3.12.13 y uv; Caddy está fijado en `2.11.4-alpine`.
El perfil Compose contiene un coordinador integrado sobre SQLite y está pensado para una sola
instancia. El modo activo/pasivo requiere desplegar dos instancias contra una base transaccional
compartida y seguir el contrato de [coordinación HA](../docs/coordinator-ha.md); no se habilita
agregando un segundo contenedor al Compose de desarrollo.

## Atajo desde la raíz

El archivo [`../docker-compose.yaml`](../docker-compose.yaml) permite ejecutar la instalación LAN
de una instancia desde la raíz del repositorio. Usa la misma imagen, volúmenes y CA local que
`compose.local-ca.yaml`; por defecto espera la clave de sesión en
`deploy/secrets/session-key`:

```bash
install -d -m 700 deploy/secrets
openssl rand -base64 48 > deploy/secrets/session-key
chmod 600 deploy/secrets/session-key
sudo chown 10001:10001 deploy/secrets/session-key
export PYFOG_PUBLIC_HOST=pyfog.lab.test

docker compose --profile admin run --rm migrate
docker compose --profile admin run --rm migrate \
  python -m pyfog create-admin --username admin
docker compose up -d
```

Para activar la publicación PXE ya verificada, definí `PYFOG_PXE_DIR` y agregá el perfil:

```bash
export PYFOG_PXE_DIR="$PWD/dist/pxe"
docker compose --profile pxe up -d
```

El DHCP/TFTP de la LAN sigue siendo externo y debe apuntar al `ipxe.efi` publicado en
`dist/pxe/tftp/`.

Compose local monta los secrets basados en archivos como bind mounts. Por eso la clave conserva
`0600`, pero debe pertenecer al UID `10001`, que es el usuario no privilegiado `pyfog` dentro de la
imagen. No uses `chmod 644` para resolver un error de permisos.

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

## LAN con Caddy local

Antes de arrancar, creá el secreto de sesión fuera del repo y definí `PYFOG_PUBLIC_HOST`:

```bash
cd deploy
install -d -m 700 secrets
openssl rand -base64 48 > secrets/session-key
chmod 600 secrets/session-key
sudo chown 10001:10001 secrets/session-key
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

## LAN con Caddy centralizado

Este escenario se usa cuando otro Caddy, en la misma red o en otro host, ya administra los
certificados y publica la IP que resuelve `PYFOG_PUBLIC_HOST`. PyFog no debe levantar el Caddy
local en ese caso: el Caddy central termina HTTPS y reenvía por una red privada a la aplicación y
al servidor estático PXE.

La topología queda así:

```text
cliente/navegador/PXE -- HTTPS --> Caddy central
                                      | /health, /login, /api -> PyFog:8000
                                      | /boot/*              -> PyFog:8080
                                      |
                                  host PyFog
```

Desde la raíz del repositorio, usá el overlay incluido:

```bash
export PYFOG_PUBLIC_HOST=pyfog.example
export PYFOG_TRUSTED_PROXY_IPS=10.10.0.20/32
export PYFOG_APP_BIND_ADDRESS=10.10.0.10
export PYFOG_PXE_BIND_ADDRESS=10.10.0.10
export PYFOG_PXE_DIR="$PWD/dist/pxe"

docker compose \
  -f docker-compose.yaml \
  -f deploy/compose.external-proxy.yaml \
  --profile admin run --rm migrate
docker compose \
  -f docker-compose.yaml \
  -f deploy/compose.external-proxy.yaml \
  --profile pxe up -d
```

Reemplazá `10.10.0.10` por la IP privada del host PyFog y `10.10.0.20` por la IP o CIDR exacto
del Caddy central. El firewall del host debe permitir los puertos `8000` y `8080` únicamente desde
el Caddy central; no los expongas a toda la LAN. El overlay pone el Caddy local en un perfil que no
se activa, publica la app en `8000` y publica el servicio estático PXE en `8080`.

La configuración del Caddy central debe conservar el hostname público y enrutar los dos paths:

```caddyfile
pyfog.example {
    handle_path /boot/* {
        reverse_proxy 10.10.0.10:8080
    }

    handle {
        reverse_proxy 10.10.0.10:8000
    }
}
```

El Caddy central debe enviar `Host` y los encabezados `X-Forwarded-*`. PyFog sólo confía en esos
encabezados desde `PYFOG_TRUSTED_PROXY_IPS`; nunca uses `0.0.0.0/0` ni `*`. El DNS de
`pyfog.example` debe apuntar al Caddy central, no directamente al host PyFog.

Si el certificado del Caddy central es público, los recolectores pueden conectarse sin `--ca-file`.
Si pertenece a una CA corporativa privada, instalá esa CA en los clientes y en el agente, o pasala
explícitamente con `--ca-file`. La CA local de Caddy sólo es necesaria en el escenario de Caddy
local con `tls internal`.

Antes de publicar PXE, reconstruí el bundle con el hostname real del Caddy central:

```bash
pxe/build-pxe build \
  --agent-dir dist/agent \
  --base-url https://pyfog.example/boot \
  --ipxe-efi /ruta/controlada/ipxe-amd64.efi \
  --output-dir dist/pxe
pxe/build-pxe verify --output-dir dist/pxe
```

El DHCP/TFTP sigue siendo externo en ambos escenarios. Debe entregar `dist/pxe/tftp/ipxe.efi`,
mientras el script iPXE descarga kernel e initramfs desde `https://pyfog.example/boot/agent/`.

## Salud, migraciones y rollback

`/health/live` sólo confirma que el proceso responde; `/health/ready` comprueba DB, almacenamiento
y el coordinador integrado. `/health/metrics` expone sólo métricas agregadas de tareas para un
colector, sin IDs ni mensajes. Caddy espera el readiness del servicio web antes de arrancar.

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
