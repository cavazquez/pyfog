# Guía operativa de PyFog

Esta guía cubre el MVP Linux de punta a punta. El alcance comprobado es Ubuntu x86_64, firmware
UEFI/GPT o BIOS/MBR sin Secure Boot, un disco por tarea, raíz ext4, ESP FAT32 sólo para UEFI y
swap opcional.
El destino debe tener igual o mayor capacidad y sector lógico compatible. PyFog no promete
compatibilidad de imágenes con FOG ni con otras distribuciones fuera de esa matriz.

## 1. Requisitos e instalación

Para desarrollo necesitás Linux, Python 3.12–3.14 y [uv](https://docs.astral.sh/uv/). Docker
Engine con Compose v2 es una alternativa cómoda. Para crear el agente PXE completo necesitás una
máquina Linux x86_64 con kernel, BusyBox, Partclone, `sgdisk`, `sfdisk`, `zstd`, `grub-pc-bin`,
`grub-efi-amd64-bin`, GRUB UEFI/BIOS y una CA
confiable; consultá [agent/README.md](../agent/README.md). QEMU/OVMF y `gdisk` son necesarios para
el laboratorio descartable.

Desarrollo:

```bash
git clone https://github.com/cavazquez/pyfog.git
cd pyfog
uv sync --frozen
make dev-up
make dev-migrate
make dev-admin PYFOG_ADMIN_USERNAME=admin
```

Abrí `http://127.0.0.1:8000`. La base y el almacén están en el volumen `pyfog_dev_data`; el
Compose de desarrollo no inicia DHCP ni toca discos físicos. `make dev-down` detiene el entorno y
`make dev-clean` borra explícitamente sólo sus datos.

Para una LAN, seguí [deploy/README.md](../deploy/README.md) y
[HTTPS/CA](https.md). Necesitás un DNS o `/etc/hosts` para el nombre del certificado, puertos HTTP
y HTTPS libres, una clave de sesión en un gestor de secretos y, si corresponde, un certificado de
servidor. Aplicá `make lan-migrate` y `make lan-admin` (o los comandos Compose equivalentes) antes
de levantar la aplicación. Las migraciones y el administrador no se crean automáticamente al
arrancar.

## 2. Red, certificados y PXE

La web y la API deben usar HTTPS fuera de loopback. El certificado debe incluir el hostname exacto;
los agentes reciben sólo la CA pública. Los puertos se configuran con `PYFOG_HTTP_PORT`,
`PYFOG_HTTPS_PORT` y `PYFOG_BIND_ADDRESS`. El perfil base no anuncia DHCP/proxyDHCP. Para PXE,
construí/verificá `dist/agent` y `dist/pxe`, elegí el servicio DHCP existente y activá de forma
deliberada:

```bash
cd deploy
export PYFOG_PXE_DIR="$PWD/../dist/pxe"
docker compose -f compose.local-ca.yaml -f compose.pxe.yaml --profile pxe up -d
```

El componente PXE sólo sirve archivos estáticos de `dist/pxe/http` por `/boot`; no recibe la base,
el almacén de imágenes ni discos. Nunca pongas tokens en DHCP, iPXE, URLs o la línea de comandos.

## 3. Registrar e inventariar

1. Ingresá como administrador y elegí **Registrar equipo**.
2. Guardá un nombre y la MAC principal, por ejemplo `52:54:00:12:34:56`.
3. En Linux, ejecutá el recolector sin root:

   ```bash
   python3 collect_inventory.py --output inventory.json
   ```

4. En la ficha del equipo, elegí **Importar inventario** y subí `inventory.json`.

También podés generar un token de 24 horas en la ficha y enviar el informe directamente:

```bash
read -rsp 'Token: ' PYFOG_INVENTORY_TOKEN; echo
export PYFOG_INVENTORY_TOKEN
python3 collect_inventory.py \
  --server https://pyfog.lab.test \
  --host-id UUID_DEL_EQUIPO \
  --output inventory.json
unset PYFOG_INVENTORY_TOKEN
```

El archivo es un informe inmutable. El informe más nuevo por fecha de recolección alimenta la ficha;
los anteriores quedan en historial. **Recibido** significa que el servidor guardó datos en una
fecha; no significa que el equipo esté conectado ahora. La pantalla **Estado** describe al servidor,
no la conectividad de cada equipo.

## 4. Preparar y capturar una referencia

La referencia UEFI debe ser Ubuntu x86_64 con GPT, ESP FAT32 y raíz ext4; la referencia BIOS debe
usar MBR, raíz ext4 y espacio libre desde el sector 2048. En ambos casos no uses LVM ni LUKS. Instalá las
aplicaciones, actualizaciones y configuración que quieras distribuir; dejá el direccionamiento en
DHCP. Comprobá el layout con el laboratorio o con el recolector antes de capturar.

En la ficha de un equipo con inventario:

1. Abrí **Capturar imagen**.
2. Elegí el disco del último inventario y creá una ficha con nombre/ descripción.
3. Confirmá explícitamente equipo, disco y lectura de bloques.
4. Arrancá el equipo por PXE con un agente `--mode imaging` y el token provisionado en
   `/run/pyfog/token` con permisos `0600`.
5. Seguí la tarea: la imagen sólo pasa a **Lista** después de validar hashes, manifiesto y
   publicación atómica.

El agente vuelve a comprobar el selector estable del disco, montajes, la tabla GPT/MBR y las herramientas justo antes
de leer. Una captura parcial queda fallida y nunca aparece como imagen desplegable.

## 5. Restaurar el origen

En la ficha del equipo de origen abrí **Restaurar**. Elegí una imagen **Lista** de ese mismo equipo
y un disco compatible del inventario actual. La pantalla muestra equipo, MAC, serie/capacidad y
fecha/origen de la imagen; marcá la confirmación de sobrescritura. El servidor vuelve a validar la
imagen, permisos, inventario, disco e idempotencia.

El agente descarga cada artefacto a staging efímero, verifica SHA-256, recrea GPT/ESP/ext4/swap,
  comprueba UUIDs y arranque UEFI fallback o GRUB BIOS, y recién entonces informa éxito. La restauración no
personaliza hostname, machine-id, claves SSH ni red: conserva la identidad del sistema de origen.

## 6. Clonar otro equipo

En la ficha de un equipo distinto del origen abrí **Clonar aquí**. Elegí la imagen lista, un disco
igual o mayor compatible y un hostname nuevo, por ejemplo `linux-aula-02`; confirmá que el disco será
sobrescrito. La tarea identifica inequívocamente **Clonación** y sólo termina después de restaurar y
personalizar.

El agente mantiene la imagen y origen intactos, genera machine-id y semilla nuevos, renueva claves
de host SSH, elimina configuraciones de red estáticas/udev persistentes y deja DHCP. Un hook de
primer arranque regenera lo necesario antes de habilitar SSH. La política está acotada a Ubuntu con
systemd y los archivos estándar descritos en [agent/README.md](../agent/README.md); configuraciones
de red o gestión externas requieren revisión manual.

## 7. Cancelación y recuperación

Una tarea en cola se cancela inmediatamente. Una tarea asignada o escribiendo primero recibe una
solicitud cooperativa: el equipo y el slot permanecen reservados hasta que el agente confirme. Si
vence la lease o se reinicia el servidor, la tarea queda **Requiere intervención**; no se crea otro
escritor automáticamente. Revisá que el agente anterior esté detenido y usá **Crear intento nuevo**.
Los eventos de intentos viejos no son válidos para continuar la tarea.

Una interrupción de restauración/clonación puede dejar el disco destino incompleto. No lo presentes
como válido: arrancá un diagnóstico, verificá el layout y repetí la operación sólo con autorización.
Las imágenes publicadas no se tocan al limpiar un staging de tarea.

## 8. Diagnóstico rápido

| Síntoma | Causa probable | Acción segura |
| --- | --- | --- |
| El equipo no aparece en PXE | DHCP/URL de bootstrap incorrectos | Revisá DHCP elegido, MAC, `dist/pxe`, DNS y logs; no habilites un segundo DHCP sin plan |
| TLS falla antes de enviar token | CA, hostname o fecha incorrectos | Instalá la CA pública correcta y usá el nombre del certificado; nunca desactives verificación |
| Imagen no aparece para desplegar | Está en borrador, fallida, eliminada o sin hashes | Abrí la ficha, verificá publicación/manifiesto y capturá de nuevo si corresponde |
| Disco incompatible | Es menor, removible, está montado o usa otro sector | Importá inventario actualizado y elegí un disco igual/mayor no montado |
| No hay espacio | El almacén no alcanza su reserva mínima o el límite de imagen | Liberá/revisá publicaciones autorizadas, comprobá **Estado** y no borres temporales activos |
| Tarea requiere intervención | Lease vencida, reinicio o pérdida de red | Detené el agente anterior, revisá el destino y reconciliá explícitamente |
| Restauración falló escribiendo | El destino puede estar incompleto | No arranques como válido; diagnosticá y repetí tras confirmar el disco |
| El servicio no está listo | DB/almacén no disponibles | Consultá `/health/live`, `/health/ready`, logs y volumen; no expongas secretos |

## 9. Backup y recuperación

Usá [backup-recovery.md](backup-recovery.md). El procedimiento copia SQLite, publicaciones y
staging con hashes; verifica referencias, manifiestos y artefactos; y restaura sólo en una
instalación vacía. Las sesiones web se invalidan, los intentos activos pasan a intervención y las
claves/certificados se recuperan de un gestor de secretos independiente. Verificá antes de volver a
habilitar tareas.
