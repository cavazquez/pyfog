# Laboratorio UEFI descartable

Este laboratorio permite probar PyFog sin tocar discos físicos ni una red de producción. Crea
dos VMs x86_64 con QEMU y OVMF, cada una con un overlay QCOW2 descartable, y las enlaza por una
red Ethernet punto a punto que solo escucha en <code>127.0.0.1</code>. No hay router, DNS, NAT ni
conexión con interfaces físicas.

| VM | MAC | Dirección aislada | Disco |
| --- | --- | --- | --- |
| <code>source</code> | <code>52:54:00:17:00:01</code> | <code>192.0.2.1/30</code> | <code>.lab/source/disk.disposable.qcow2</code> |
| <code>target</code> | <code>52:54:00:17:00:02</code> | <code>192.0.2.2/30</code> | <code>.lab/target/disk.disposable.qcow2</code> |

Los nombres de los discos incluyen <code>disposable</code> y son overlays de solo lectura sobre la
imagen de referencia. Nunca se pasa un dispositivo físico a QEMU.

## Prerrequisitos

En Ubuntu:

    sudo apt-get install qemu-system-x86 qemu-utils ovmf cloud-image-utils file gdisk socat
    ./lab/pyfog-lab doctor

OVMF se usa sin Secure Boot. QEMU usa KVM si el host lo permite y emulación TCG en caso contrario;
TCG es más lento, pero conserva el comportamiento aislado.

## Crear, arrancar e inspeccionar

    ./lab/pyfog-lab up
    ./lab/pyfog-lab status
    ./lab/pyfog-lab console source

La primera ejecución descarga la publicación inmutable <code>20260911</code> de Ubuntu Server 24.04
LTS para AMD64. El manifiesto [reference-image.env](reference-image.env) fija su SHA-256, tomado del
[manifiesto de Canonical](https://cloud-images.ubuntu.com/noble/20260911/SHA256SUMS), y la descarga
se rechaza si no coincide. Cualquier actualización de imagen requiere revisar intencionalmente URL y
hash.

El usuario temporal de las dos VMs es <code>pyfog</code>. La contraseña se genera localmente y solo
se guarda con permisos <code>0600</code> en <code>.lab/credentials.env</code>; se elimina al destruir
el laboratorio. Para verla desde el host:

    sed -n 's/^PYFOG_LAB_PASSWORD=//p' .lab/credentials.env

La consola serie permite observar el boot y entrar para ejecutar, por ejemplo:

    ip address show lab0
    ping -c 2 192.0.2.2

Para salir de la consola <code>socat</code>, usá <code>Ctrl-]</code>. El monitor QMP queda en un
socket Unix privado:

    ./lab/pyfog-lab monitor source

Allí se puede enviar primero <code>{"execute":"qmp_capabilities"}</code> y luego
<code>{"execute":"query-status"}</code>. Para ver el disco, MAC e IP que QEMU recibió:

    ./lab/pyfog-lab inspect target

Cada arranque también se conserva en <code>.lab/source/serial.log</code> o
<code>.lab/target/serial.log</code>, incluso si todavía no había una consola conectada.

También se puede preparar sin arrancar, o iniciar cada VM por separado:

    ./lab/pyfog-lab create
    ./lab/pyfog-lab start source
    ./lab/pyfog-lab start target

<code>target</code> exige que <code>source</code> ya esté activa, porque esta última abre el extremo
local del enlace aislado.

## Layout Linux de referencia

Antes de crear los overlays, el script convierte temporalmente la imagen dentro de
<code>.lab/cache/</code> y comprueba:

- tabla de particiones GPT;
- partición EFI System con código <code>EF00</code> y FAT32;
- al menos un sistema de archivos ext4;
- ausencia de LVM y LUKS.

Se puede ejecutar la comprobación sola con:

    ./lab/pyfog-lab validate-layout

## Prueba E2E reproducible

El runner [`e2e.sh`](e2e.sh) combina el contrato de tareas/agente con un smoke real de arranque UEFI:

    ./lab/e2e.sh plan
    ./lab/e2e.sh run

`run` exige las herramientas de `doctor`, ejecuta las pruebas de captura, restauración, clonación,
cancelación y reconciliación con una base temporal, y luego arranca `source` y `target` con OVMF
sobre los overlays del laboratorio. Guarda versión de commit, versiones de herramientas, resultados,
logs de pytest y consolas en `.e2e/run.*`; no recibe tokens por argumentos ni los escribe en esos
archivos. Un fallo conserva los recursos para diagnóstico; después de revisar el informe usá
`./lab/pyfog-lab destroy`.

El smoke espera hasta 120 segundos por el prompt Linux de ambas VMs en la salida serial. Se puede ajustar para
hosts lentos con `PYFOG_E2E_UEFI_TIMEOUT_SECONDS=240 ./lab/e2e.sh run`.

La prueba de disco usa solamente los recursos marcados por el laboratorio. El runner no acepta ni
monta una ruta de bloque del host y el perfil PXE no agrega DHCP. En un host sin QEMU/OVMF se puede
usar `plan` para revisar la matriz, pero `run` debe ejecutarse en una VM Linux equipada antes de
afirmar una validación UEFI completa.

La referencia y los overlays quedan fuera de Git mediante <code>.gitignore</code>.

## Detener y limpiar

    ./lab/pyfog-lab stop all
    ./lab/pyfog-lab destroy

<code>destroy</code> primero verifica el marcador <code>.lab/.pyfog-lab-marker</code>, solo envía
señales a procesos cuyo PID y línea de comandos identifican el QEMU de este laboratorio, y borra
únicamente <code>cache</code>, <code>source</code>, <code>target</code>, la credencial efímera y el
marcador bajo <code>.lab/</code>. Si encuentra una entrada desconocida en <code>.lab/</code>, la
conserva y no elimina el directorio contenedor.
