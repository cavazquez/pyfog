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

La capacidad declarada debe coincidir con el firmware y los filesystems del layout. El perfil
actual admite UEFI sin Secure Boot, GPT, un disco, FAT32/ext4/swap, sin cifrado y volúmenes por
particiones. BIOS/MBR, Secure Boot, LVM, RAID, LUKS, otros filesystems y varios discos se rechazan
antes de iniciar una escritura.

El catálogo comprueba el manifiesto y las capacidades antes de mostrar una imagen como utilizable.
La validación de creación de tareas vuelve a comprobarlas, y el agente las valida inmediatamente
antes de leer o escribir el destino. Un manifiesto con forma válida pero capacidades no soportadas
produce un error explícito y no se publica ni se restaura.

## Layout y artefactos

El manifiesto registra el UUID de imagen, fecha de creación, algoritmo `sha256`, equipo e informe
de inventario de origen, sistema Ubuntu, arquitectura `x86_64`, firmware, herramienta Partclone,
capacidades y un disco GPT. El disco conserva capacidad total, sector lógico, cantidad de sectores,
GUID GPT y rango utilizable. Cada partición registra número, rol, sector inicial, cantidad de
sectores, GUID, filesystem, UUID, punto de montaje y artefacto:

* `esp`: FAT32, `/boot/efi`, con UUID FAT32 de ocho dígitos y `partclone.fat`;
* `boot`: ext4, `/boot`, opcional, con `partclone.ext4`;
* `root`: ext4, `/`, obligatorio, con `partclone.ext4`;
* `swap`: swap, sin punto de montaje ni artefacto de datos, opcional.

Los artefactos sólo pueden ser archivos relativos POSIX dentro del directorio de la imagen. Cada
uno registra tamaño, compresión (`none`, `gzip` o `zstd`) y SHA-256 hexadecimal en minúsculas.
Todas las rutas se referencian exactamente una vez desde una partición de datos; swap queda
explícita para conservar su UUID sin copiar sus bloques.

El validador comprueba la capacidad, el rango GPT, las particiones no superpuestas, las UUID,
referencias, rutas, tamaños, sumas y capacidades. Los campos desconocidos, el JSON inválido, los
artefactos ausentes o alterados y los perfiles incompatibles se rechazan.

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
