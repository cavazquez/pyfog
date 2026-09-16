# Agente Linux de PyFog

Este directorio contiene el agente efímero que se arranca por PXE. El agente no monta discos ni
escribe dispositivos: en su modo predeterminado obtiene un inventario de solo lectura, lo guarda
en el `tmpfs` del initramfs y, si se proporcionan servidor, equipo y una capacidad temporal, lo
envía por HTTPS.

## Construir

La construcción se ejecuta en una máquina Linux x86_64 con las herramientas que se empaquetarán.
El modo de imagen exige `sgdisk`, `partclone.ext4` y `partclone.fat` (también se acepta
`partclone.vfat`). El modo de inventario es útil para probar el arranque y no incluye herramientas
de escritura de imágenes.

```bash
agent/build-agent doctor --mode inventory-only
agent/build-agent build --mode inventory-only --output-dir dist/agent
agent/build-agent verify --output-dir dist/agent
```

Para el agente completo:

```bash
agent/build-agent doctor --mode imaging
agent/build-agent build --mode imaging --output-dir dist/agent
agent/build-agent verify --output-dir dist/agent
```

Se puede fijar `SOURCE_DATE_EPOCH` o pasar `--source-date-epoch` para reproducir el archivo. Si no
se indica, se usa `1704067200` (2024-01-01 00:00:00 UTC). El kernel se descubre en `/boot` o se
puede indicar con `--kernel`. `--modules-dir` permite seleccionar los módulos de otra instalación;
por defecto se usa `/lib/modules/<versión>`.

El builder incorpora el bundle del sistema como confianza TLS. Para una instalación con CA propia,
pasá `--ca-file /ruta/ca.pem`; quedará en `/etc/pyfog/ca.pem` y su hash aparecerá en el manifiesto.
El arranque debe indicar `pyfog.ca_file=/etc/pyfog/ca.pem`.

La salida contiene:

* `vmlinuz`: el kernel que se verificará antes de publicarlo;
* `initramfs.img`: cpio `newc` comprimido con gzip, con fechas y propietarios reproducibles;
* `manifest.json`: versiones, procedencia, capacidades y SHA-256 de artefactos y archivos;
* `SHA256SUMS`: sumas de los tres archivos publicables.

El manifiesto se genera fuera del initramfs para evitar un hash circular. Dentro del agente queda
una copia de los metadatos en `/etc/pyfog-agent.json`.

## Parámetros de arranque

El initramfs lee parámetros `pyfog.*` de `/proc/cmdline`:

| Parámetro | Valor | Uso |
| --- | --- | --- |
| `pyfog.mode` | `inventory`, `shell` o `halt` | `inventory` es el predeterminado. `shell` queda para diagnóstico. |
| `pyfog.net` | `dhcp` o `none` | DHCP sólo levanta interfaces no-loopback; no monta discos. |
| `pyfog.server` | URL base | Sólo se usa con HTTPS fuera de loopback. |
| `pyfog.host_id` | UUID | Equipo al que pertenece el informe. |
| `pyfog.token_file` | ruta en el initramfs | Archivo temporal con el Bearer token; nunca se pone el secreto en la línea de comandos. |
| `pyfog.ca_file` | ruta | CA pública de la instalación, por ejemplo `/etc/ssl/certs/ca-certificates.crt`. |
| `pyfog.keep_alive` | `1` | Mantiene un shell después del inventario para pruebas. |

Sin servidor el inventario queda en `/run/pyfog/inventory.json`; ese archivo vive en memoria y se
elimina al apagar el agente. Al terminar, el agente apaga la VM. `pyfog.mode=shell` no ejecuta
ninguna operación de bloques.

La imagen de arranque no toma paquetes de la red durante la ejecución. La versión, el kernel, las
herramientas y sus hashes efectivos deben revisarse en `manifest.json` antes de publicar los
artefactos en el servidor PXE.
