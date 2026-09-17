# Agente Linux de PyFog

Este directorio contiene el agente efímero que se arranca por PXE. En su modo predeterminado no
monta discos ni escribe dispositivos: obtiene un inventario de solo lectura, lo guarda en el
`tmpfs` del initramfs y puede solicitar la aprobación administrativa del equipo antes de enviarlo
por HTTPS. El modo `imaging` también evita montar y escribir el disco de origen; lee las particiones
admitidas con Partclone y ejecuta la operación de imagen previamente encolada: captura,
restauración o clonación.

## Construir

La construcción se ejecuta en una máquina Linux x86_64 con las herramientas que se empaquetarán.
El modo de imagen exige `sgdisk`, `partclone.ext4` y `partclone.fat` (también se acepta
`partclone.vfat`), `zstd`, y las herramientas de montaje y arranque UEFI. El modo de inventario es
útil para probar el arranque y no incluye herramientas de escritura de imágenes.

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
| `pyfog.mode` | `inventory`, `imaging`, `shell` o `halt` | `inventory` es el predeterminado. `imaging` ejecuta la tarea encolada (captura, restauración o clonación); `shell` queda para diagnóstico. |
| `pyfog.net` | `dhcp` o `none` | DHCP sólo levanta interfaces no-loopback; no monta discos. |
| `pyfog.server` | URL base | Sólo se usa con HTTPS fuera de loopback. |
| `pyfog.host_id` | UUID | Equipo al que pertenece el informe. |
| `pyfog.pair` | `1` | Solicita emparejamiento PXE y espera aprobación en la web; no requiere token previo. |
| `pyfog.token_file` | ruta en el initramfs | Archivo temporal con el Bearer token; nunca se pone el secreto en la línea de comandos. |
| `pyfog.ca_file` | ruta | CA pública de la instalación, por ejemplo `/etc/ssl/certs/ca-certificates.crt`. |
| `pyfog.keep_alive` | `1` | Mantiene un shell después del inventario para pruebas. |

Sin servidor el inventario queda en `/run/pyfog/inventory.json`; ese archivo vive en memoria y se
elimina al apagar el agente. Al terminar, el agente apaga la VM. `pyfog.mode=shell` no ejecuta
ninguna operación de bloques.

Con `pyfog.pair=1`, el recolector genera un desafío efímero visible en la consola, crea una
solicitud en **Descubiertos** y consulta su estado hasta que un administrador la aprueba o rechaza.
La capacidad de emparejamiento se mantiene sólo en memoria, se consume al enviar el primer
inventario y no permite iniciar tareas de imagen. Para capturas se usa el token vigente del equipo;
la API lo cambia por una capacidad limitada al intento reclamado.

## Ejecutar una operación de imagen

Encolá primero la captura, restauración o clonación desde la web. Construí el agente completo con
`--mode imaging`, revisá sus cuatro artefactos y arrancalo con parámetros equivalentes a:

```text
pyfog.mode=imaging pyfog.net=dhcp pyfog.server=https://pyfog.example
pyfog.host_id=UUID_DEL_EQUIPO pyfog.token_file=/run/pyfog/token
pyfog.ca_file=/etc/pyfog/ca.pem
```

Un mecanismo de provisión controlado debe crear `/run/pyfog/token` en el initramfs con el token del
equipo y permisos `0600`. El secreto no debe aparecer en la línea de comandos, DHCP, iPXE o una
URL. El agente reclama como máximo una tarea compatible, valida nuevamente el disco y el layout,
sube cada artefacto por fragmentos, o descarga y verifica los artefactos publicados, y confirma el
resultado al finalizar. La restauración recrea GPT, ESP, `/boot`, raíz y swap opcional, comprueba el
fallback UEFI y nunca crea entradas NVRAM. La clonación además elimina la identidad heredada,
instala DHCP y genera machine-id y claves SSH en el primer arranque. Si una lease vence, el servidor
conserva la tarea para intervención y el agente no intenta reanudarla automáticamente.

La imagen de arranque no toma paquetes de la red durante la ejecución. La versión, el kernel, las
herramientas y sus hashes efectivos deben revisarse en `manifest.json` antes de publicar los
artefactos en el servidor PXE.
