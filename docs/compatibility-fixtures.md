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

La matriz contiene dos perfiles habilitados: UEFI sin Secure Boot/GPT con FAT32/ext4 y BIOS sin
Secure Boot/MBR con ext4. Ambos exigen un disco, sin cifrado y volúmenes por particiones. El
fixture BIOS incluye una tabla DOS determinista, firma de disco, partición desde el sector 2048 y
firma `55aa`; Secure Boot, XFS, LVM, LUKS2, RAID1 y múltiples discos siguen siendo negativos.

`tests/test_compatibility_fixtures.py` valida la matriz, genera los discos dos veces, comprueba sus
hashes y verifica que el catálogo/contrato del agente acepte los perfiles UEFI/BIOS y rechace cada
perfil incompatible antes de la restauración. No abre dispositivos de bloque ni usa credenciales.
Los perfiles quedan marcados para captura, restauración y boot; `lab/e2e.sh --bios` ejecuta el
smoke real BIOS/MBR con SeaBIOS y `lab/e2e.sh` ejecuta el laboratorio UEFI.

CI instala `qemu-utils` y ejecuta `make compatibility-check`. El diagnóstico conserva el ID del
fixture y la diferencia de checksum/tamaño cuando una definición deja de ser reproducible.
