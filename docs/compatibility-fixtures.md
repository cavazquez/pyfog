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

La matriz QCOW2 conserva dos perfiles de disco completos y arrancables: UEFI sin Secure Boot/GPT
con FAT32/ext4 y BIOS sin Secure Boot/MBR con ext4. El fixture BIOS incluye una tabla DOS
determinista, firma de disco, partición desde el sector 2048 y firma `55aa`. Los perfiles XFS,
Btrfs, LVM, LUKS2, RAID1 y multidisco no se simulan como QCOW2 arrancables porque requieren
stacks de block devices (o un proveedor de clave) que no deben tocarse en CI; sus contratos de
manifiesto, preflight y restauración se cubren con fixtures JSON y pruebas unitarias dirigidas.

`tests/test_compatibility_fixtures.py` valida la matriz, genera los discos dos veces, comprueba sus
hashes y verifica la compatibilidad base antes de la restauración. No abre dispositivos de bloque
ni usa credenciales. Los perfiles extendidos tienen pruebas específicas en `test_restore.py`,
`test_image_manifest.py` y el agente; `lab/e2e.sh --bios` ejecuta el smoke real BIOS/MBR con
SeaBIOS y `lab/e2e.sh` ejecuta el laboratorio UEFI.

CI instala `qemu-utils` y ejecuta `make compatibility-check`. El diagnóstico conserva el ID del
fixture y la diferencia de checksum/tamaño cuando una definición deja de ser reproducible.
