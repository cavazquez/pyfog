# HTTPS, CA local y proxy de PyFog

El servidor de producción se publica sólo a través de Caddy. El proceso FastAPI escucha dentro de
la red Docker y acepta `X-Forwarded-*` únicamente desde la subred privada configurada. Caddy fija
esos encabezados y descarta valores enviados por el cliente; FastAPI usa el esquema HTTPS resultante
para evitar un bucle de redirección y marcar las cookies como `Secure`.

No hay CORS habilitado: la web y la API se usan desde el mismo origen. Configurar un origen cruzado
no forma parte del MVP y no debe hacerse para sortear una integración de agente.

## Laboratorio con CA local

Elegí un nombre DNS de laboratorio que resuelva al servidor, por ejemplo `pyfog.lab.test`. En cada
cliente de laboratorio agregalo a DNS o a `/etc/hosts`; no uses una IP en la URL, porque el nombre
forma parte de la verificación TLS.

```bash
cd deploy
install -d -m 700 secrets
openssl rand -base64 48 > secrets/session-key
chmod 600 secrets/session-key
export PYFOG_PUBLIC_HOST=pyfog.lab.test
export PYFOG_SESSION_KEY_FILE="$PWD/secrets/session-key"

docker compose -f compose.local-ca.yaml run --rm app python -m pyfog init-db
docker compose -f compose.local-ca.yaml run --rm app python -m pyfog create-admin --username admin
docker compose -f compose.local-ca.yaml up -d
```

El puerto 80 sólo redirige al mismo host por HTTPS. No se deben enviar tokens de inventario, archivos
de imagen ni contraseñas a una URL `http://`; el recolector ya rechaza HTTP fuera de loopback.

Caddy genera una CA interna para este laboratorio. Copiá únicamente su certificado público a los
clientes y al initramfs de imagen; la clave de la CA y la clave TLS del servidor permanecen en el volumen
privado de Caddy y nunca se montan en el contenedor de PyFog:

```bash
docker compose -f compose.local-ca.yaml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt ./pyfog-lab-root.crt
openssl x509 -in pyfog-lab-root.crt -noout -subject -issuer -enddate -fingerprint -sha256
```

El recolector y el agente de imagen validan cadena, nombre y vigencia con el verificador estándar de
Python. Para la CA local, entregá ese archivo público mediante un canal controlado por el administrador:

```bash
python3 collect_inventory.py \
  --server https://pyfog.lab.test \
  --ca-file pyfog-lab-root.crt \
  --host-id UUID_DEL_EQUIPO
```

No existe una opción para desactivar la validación TLS. Una CA desconocida o un nombre diferente al
del certificado debe fallar antes de que se transmita el Bearer token.

## Certificado propio en la LAN

Para una CA corporativa o un certificado emitido por otra CA, prepará un PEM de cadena completa cuyo
SAN incluya exactamente `PYFOG_PUBLIC_HOST`, y una clave privada legible sólo por quien despliega
Caddy. No copies ambos archivos al repositorio ni al initramfs.

```bash
export PYFOG_TLS_CERTIFICATE_FILE=/ruta/segura/server-fullchain.pem
export PYFOG_TLS_PRIVATE_KEY_FILE=/ruta/segura/server-key.pem
docker compose \
  -f compose.local-ca.yaml \
  -f compose.custom-certificate.yaml \
  up -d
```

El archivo de certificado y la clave se montan como secretos sólo en Caddy. La aplicación recibe
solamente la clave de sesión mediante otro secreto; los agentes reciben sólo la CA pública que les
permite verificar al servidor.

## Arranque PXE

TFTP/PXE es un bootstrap sin confianza. Transporta iPXE, kernel, initramfs y, si hace falta, la CA
pública. No transporta contraseñas, tokens persistentes, imágenes ni claves privadas. El agente usa
HTTPS por el nombre configurado antes de solicitar el registro; un error de red o certificado
termina en diagnóstico y retorno al disco local, nunca en una operación de disco. El perfil
reproducible, el layout de TFTP/HTTPS y la configuración de DHCP existente están documentados en
[pxe/README.md](../pxe/README.md).

## Verificación y operación

Con la CA de laboratorio instalada, la conexión correcta debe responder y la conexión con otro
nombre debe fallar:

```bash
curl --fail --cacert pyfog-lab-root.crt https://pyfog.lab.test/health
curl --fail --cacert pyfog-lab-root.crt \
  --resolve nombre-incorrecto.lab.test:443:IP_DEL_SERVIDOR \
  https://nombre-incorrecto.lab.test/health
```

La segunda orden debe terminar con un error TLS de nombre o handshake. Revisá el certificado y la
resolución DNS en vez de añadir una excepción insegura. Antes de actualizar imágenes, validá el
archivo con `docker compose ... config`; el runtime no usa `DEBUG`, requiere hosts explícitos, una
clave de sesión de al menos 32 caracteres y una lista explícita de proxies confiables.

La sintaxis `tls internal` y el manejo de CA local siguen la
[documentación de Caddy](https://caddyserver.com/docs/caddyfile/directives/tls). Caddy establece los
encabezados `X-Forwarded-*` del proxy y no confía en valores proporcionados por clientes, según su
[documentación de reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
