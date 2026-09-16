# Manifiesto de imagen v1

PyFog usa un formato propio para que una restauración pueda comprobar el layout antes de escribir
un destino. No es un formato compatible con FOG ni con Clonezilla. El archivo se publica junto a
los artefactos de las particiones y se valida antes de entregarlo a un agente.

La pantalla **Imágenes** crea la ficha y su UUID antes de capturar. La ficha empieza en `draft`;
la captura futura la asociará con este manifiesto y actualizará estado, origen, tamaño y
compatibilidad. Sólo una ficha `ready` con manifiesto y verificación de integridad puede elegirse
para restaurar.

## Contrato

El documento raíz tiene `format: "pyfog-disk-image"` y `format_version: 1`. Registra el UUID de la
imagen, fecha de creación, algoritmo `sha256`, equipo e informe de inventario de origen, sistema
Ubuntu, arquitectura `x86_64`, firmware UEFI con Secure Boot desactivado y la herramienta Partclone
con su versión y comandos.

`disk` conserva la capacidad total en bytes, el tamaño de sector lógico, la cantidad de sectores,
el GUID del disco GPT y el rango utilizable. Cada partición registra número, rol, sector inicial,
cantidad de sectores, GUID de partición, filesystem, UUID y punto de montaje:

* `esp`: FAT32, `/boot/efi`, con un UUID FAT32 de ocho dígitos y un artefacto `partclone.fat`;
* `boot`: ext4, `/boot`, opcional, con artefacto `partclone.ext4`;
* `root`: ext4, `/`, obligatorio, con artefacto `partclone.ext4`;
* `swap`: swap, sin punto de montaje ni artefacto de datos, opcional.

Los artefactos sólo pueden ser archivos relativos POSIX dentro del directorio de la imagen. Cada
uno registra tamaño en bytes, compresión (`none`, `gzip` o `zstd`) y SHA-256 hexadecimal en
minúsculas. Todas las rutas se referencian exactamente una vez desde una partición de datos; swap
queda explícita para conservar su UUID sin copiar sus bloques.

El validador comprueba que la capacidad sea exactamente `sector_count × logical_sector_bytes`,
que el rango GPT y todas las particiones estén dentro del disco, que no se superpongan y que exista
una única ESP y raíz. También rechaza UUIDs incompatibles, sistemas distintos de Ubuntu, campos
desconocidos, rutas de escape, tamaños diferentes y artefactos ausentes o alterados.

## Validar una imagen

Con sólo el manifiesto:

```bash
python3 -m scripts.validate_image_manifest /srv/pyfog/images/IMAGE/manifest.json
```

Para comprobar los tamaños y las sumas contra la publicación:

```bash
python3 -m scripts.validate_image_manifest \
  /srv/pyfog/images/IMAGE/manifest.json \
  --artifacts-dir /srv/pyfog/images/IMAGE
```

La captura futura deberá escribir en un directorio temporal, generar este manifiesto, verificarlo
con el mismo código y publicarlo sólo después de que todas las comprobaciones pasen. Una
restauración o clonación debe volver a validar el manifiesto y la capacidad del destino antes de
abrir cualquier dispositivo de bloques.
