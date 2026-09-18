# Manifiesto de imagen v2

PyFog usa un formato propio para que una restauración pueda comprobar el layout y la
compatibilidad antes de escribir un destino. No es compatible con FOG ni con Clonezilla.

El archivo se publica junto a los artefactos de las particiones y se valida antes de entregarlo a
un agente. Sólo una ficha `ready` con manifiesto, capacidades soportadas y verificación de
integridad puede elegirse para restaurar o clonar.

## Versiones y compatibilidad

El documento raíz conserva `format: "pyfog-disk-image"`. La versión actual es `format_version: 2`.
Las imágenes v1 siguen siendo legibles: el validador deriva sus capacidades implícitas y
`upgrade_manifest()` puede generar una representación v2 sin cambiar la identidad, geometría ni
artefactos de la imagen.

Una imagen v2 agrega `capabilities`:

```json
{
  "firmware": {"type": "uefi", "secure_boot": false},
  "partition_table": "gpt",
  "disks": 1,
  "filesystems": ["ext4", "fat32"],
  "encryption": "none",
  "volumes": "partitions"
}
```

La capacidad declarada debe coincidir con el firmware y los filesystems del layout. El perfil base
conserva UEFI sin Secure Boot con GPT/FAT32/ext4/swap, o BIOS sin Secure Boot con MBR/ext4 (swap
opcional). La validación extendida v2 agrega XFS y Btrfs, varios discos con identidades estables,
LVM lineal, RAID1 mdadm y LUKS2; estos perfiles se habilitan sólo cuando el agente anuncia sus
herramientas y el preflight termina antes de modificar el destino. Secure Boot y combinaciones de
LUKS2 con LVM/RAID siguen rechazados.

El catálogo comprueba el manifiesto y las capacidades antes de mostrar una imagen como utilizable.
La validación de creación de tareas vuelve a comprobarlas, y el agente las valida inmediatamente
antes de leer o escribir el destino. Un manifiesto con forma válida pero capacidades no soportadas
produce un error explícito y no se publica ni se restaura.

## Layout y artefactos

El manifiesto registra el UUID de imagen, fecha de creación, algoritmo `sha256`, equipo e informe
de inventario de origen, sistema Ubuntu, arquitectura `x86_64`, firmware, herramienta Partclone,
capacidades y uno o más discos GPT o MBR. El disco conserva capacidad total, sector lógico,
cantidad de sectores y, según el perfil, GUID/rango GPT o firma MBR. Cada partición registra número,
rol, sector inicial, cantidad de sectores, filesystem, UUID, punto de montaje y artefacto; sólo GPT
agrega GUID de partición:

* `esp`: FAT32, `/boot/efi`, con UUID FAT32 de ocho dígitos y `partclone.fat`;
* `boot`: ext4, `/boot`, opcional, con `partclone.ext4`;
* `root`: ext4, XFS o Btrfs, `/`, obligatorio, con la herramienta Partclone correspondiente;
* `swap`: swap, sin punto de montaje ni artefacto de datos, opcional.

LVM lineal agrega `volumes` con PV/VG/LV y captura el filesystem lógico; LUKS2 agrega sólo UUID,
cipher y sector size, nunca una clave. RAID1 agrega `raid_arrays` con UUID mdadm, metadata, tamaño y
el orden de `member_ids`; todos los miembros comparten el artefacto raíz verificado y la restauración
lo escribe una sola vez sobre el array creado. La expansión sólo se permite para el layout por
particiones con raíz ext4 y destino mayor. Btrfs conserva el subvolumen raíz y sus opciones de
montaje declaradas.

En el perfil BIOS/MBR el disco agrega `boot_sector`, un artefacto `boot-sector.bin` sin compresión
de exactamente 446 bytes. La captura conserva el código del sector 0, valida la firma `55aa` y
exige que la primera partición empiece en el sector 2048 para dejar espacio al embedding de GRUB
BIOS. La restauración escribe ese código sin tocar la tabla MBR, reinstala GRUB con
`grub-install --target=i386-pc` y vuelve a verificar el sector antes de informar éxito. Un perfil
UEFI nunca acepta ese artefacto, y un perfil BIOS nunca acepta ESP/GPT.

Los artefactos sólo pueden ser archivos relativos POSIX dentro del directorio de la imagen. Cada
uno registra tamaño, compresión (`none`, `gzip` o `zstd`) y SHA-256 hexadecimal en minúsculas.
Todas las rutas se referencian exactamente una vez desde una partición de datos, excepto el artefacto
raíz compartido por los miembros de RAID1; swap queda explícita para conservar su UUID sin copiar
sus bloques.

El validador comprueba la capacidad, el rango GPT o el espacio seguro MBR, las particiones no
superpuestas, las UUID, referencias, rutas, tamaños, sumas y capacidades. Los campos desconocidos,
el JSON inválido, los artefactos ausentes o alterados y los perfiles incompatibles se rechazan.

La matriz reproducible de perfiles y sus QCOW2 descartables está en
[`docs/compatibility-fixtures.md`](compatibility-fixtures.md).

## Validar una imagen

Con sólo el manifiesto:

```bash
python3 -m scripts.validate_image_manifest /srv/pyfog/images/IMAGE/manifest.json
```

Para comprobar tamaños y sumas contra la publicación:

```bash
python3 -m scripts.validate_image_manifest \
  /srv/pyfog/images/IMAGE/manifest.json \
  --artifacts-dir /srv/pyfog/images/IMAGE
```

La captura escribe primero en staging privado, genera v2, verifica el contenido con el mismo
código y publica mediante renombre atómico sólo después de que todas las comprobaciones pasen.
Una restauración o clonación vuelve a validar versión, capacidades, manifiesto y capacidad del
destino antes de abrir cualquier dispositivo de bloques.
