# Perfil PXE/UEFI de PyFog

`build-pxe` publica un perfil iPXE junto con los artefactos del agente ya verificados. El perfil
ofrece dos opciones: inventario de solo lectura o disco local. El valor predeterminado es el disco
local después de un timeout de cinco segundos. Los errores de DHCP, HTTPS, descarga o arranque van
a `local` una sola vez; no hay `chain` recursivo ni una ruta que escriba dispositivos.

El repositorio no contiene un binario iPXE precompilado. Es intencional: el administrador debe
obtener o compilar `ipxe.efi` para su política de firmware, revisar su SHA-256 y pasarlo al builder.
El hash queda en el manifiesto del perfil. El builder puede crear un perfil sin ese archivo con
`--profile-only` para revisar el script antes de configurar TFTP.

## Generar un perfil

Primero construí y verificá el agente:

```bash
agent/build-agent build --mode inventory-only --output-dir dist/agent
agent/build-agent verify --output-dir dist/agent
```

Luego generá la publicación PXE. La URL es la raíz HTTPS que Caddy expondrá; no admite credenciales,
query ni fragmentos:

```bash
pxe/build-pxe doctor
pxe/build-pxe build \
  --agent-dir dist/agent \
  --base-url https://pyfog.example/boot \
  --ipxe-efi /ruta/controlada/ipxe.efi \
  --output-dir dist/pxe
pxe/build-pxe verify --output-dir dist/pxe
```

La salida tiene esta forma:

```text
dist/pxe/
├── manifest.json       # hashes del perfil, iPXE, kernel e initramfs
├── SHA256SUMS
├── tftp/
│   ├── boot.ipxe
│   └── ipxe.efi
└── http/agent/
    ├── vmlinuz
    ├── initramfs.img
    ├── manifest.json
    └── SHA256SUMS
```

`http/` es la raíz del servidor HTTPS y `tftp/` la raíz TFTP. Sólo se copian los cuatro archivos
publicables del agente; nunca se incluyen tokens, contraseñas, claves privadas ni el directorio de
trabajo del builder.

## DHCP existente y TFTP

PyFog no inicia ni reemplaza el DHCP de la LAN. En el DHCP administrado agregá, para clientes
x86_64 UEFI, el `next-server` del servicio TFTP y el nombre `ipxe.efi`:

```text
next-server 192.0.2.10;
filename "ipxe.efi";
```

Publicá `dist/pxe/tftp/` por TFTP de sólo lectura. El binario iPXE descarga `boot.ipxe` desde el
servidor HTTPS o podés configurar el DHCP para entregarlo como script inicial; el script generado
descarga el kernel/initramfs desde `${base-url}/agent/`. No entregues el token del equipo ni una
capacidad de tarea por DHCP o por la URL.

Para Caddy, agregá un `handle_path` antes del `reverse_proxy` (el resto de la configuración de
[`docs/https.md`](../docs/https.md) permanece igual):

```caddyfile
handle_path /boot/* {
	root * /srv/pyfog/pxe/http
	header Cache-Control "no-store"
	file_server
}

handle {
	reverse_proxy app:8000
}
```

El bloque listo para copiar también está en [`Caddyfile.snippet`](Caddyfile.snippet).

Montá `dist/pxe/http` como `/srv/pyfog/pxe/http:ro` en Caddy y mantené el certificado con el nombre
que usa `--base-url`. En una CA local, el iPXE debe confiar en esa CA; el agente también puede llevar
la CA pública dentro de su initramfs (`agent/build-agent --ca-file ...`) y vuelve a verificar el
certificado con Python antes de enviar el inventario.

## Validación

Revisá `manifest.json` y `SHA256SUMS` antes de copiar los directorios a TFTP/HTTPS. Probá primero
una VM UEFI sin un disco de producción: elegí inventario, cortá el servidor y confirmá que el perfil
termina en el disco local. La opción `local` no descarga el agente y el fallo de una descarga no
salta a otra URL.
