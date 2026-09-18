# Matriz de compatibilidad y fixtures

La matriz de [`tests/fixtures/compatibility/matrix.json`](../tests/fixtures/compatibility/matrix.json)
define los perfiles de disco que el contrato debe reconocer. Cada entrada declara firmware, tabla
de particiones, cantidad de discos, filesystems, cifrado, volúmenes, tamaño virtual QCOW2, SHA-256,
checks esperados y restricciones.

Los QCOW2 no se versionan como binarios. El generador
[`scripts/compatibility_fixtures.py`](../scripts/compatibility_fixtures.py) los crea en un directorio
temporal con `qemu-img`, opciones fijas (`compat=1.1`, clusters de 64 KiB y `lazy_refcounts=off`) y
verifica formato, tamaño, opciones y checksum. Dos ejecuciones producen los mismos bytes. Para
reproducirlos localmente:

```bash
tmp_dir="$(mktemp -d)"
uv run python -m scripts.compatibility_fixtures --output-dir "$tmp_dir"
qemu-img info --output=json "$tmp_dir/uefi-ext4.qcow2"
rm -rf -- "$tmp_dir"
```

La matriz contiene un único perfil habilitado: UEFI sin Secure Boot, GPT, un disco, FAT32/ext4,
sin cifrado y volúmenes por particiones. Los fixtures BIOS/MBR, Secure Boot, XFS, LVM, LUKS2,
RAID1 y múltiples discos son negativos: prueban detección y rechazo seguro, pero no habilitan la
capacidad correspondiente.

`tests/test_compatibility_fixtures.py` valida la matriz, genera los discos dos veces, comprueba sus
hashes y verifica que el catálogo/contrato del agente rechace cada perfil incompatible antes de
la restauración. No abre dispositivos de bloque ni usa credenciales. El perfil UEFI/ext4 queda
además marcado para captura, restauración y smoke de arranque; el laboratorio UEFI de
[`lab/e2e.sh`](../lab/e2e.sh) es el que ejecuta ese arranque real.

CI instala `qemu-utils` y ejecuta `make compatibility-check`. El diagnóstico conserva el ID del
fixture y la diferencia de checksum/tamaño cuando una definición deja de ser reproducible.
