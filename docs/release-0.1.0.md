# Preparar PyFog v0.1.0

Este documento prepara una entrega verificable; no crea ni publica un tag o un GitHub Release por sí
solo. La publicación externa requiere una decisión del mantenedor después de revisar el checklist.

## Checklist reproducible

Desde un checkout limpio:

```bash
uv sync --frozen
make check
make audit
make release-check
make release-package
```

`release-check` exige que `pyproject.toml`, `pyfog/app.py` y `agent/VERSION` declaren la misma
versión y comprueba la documentación, licencia y builders requeridos. Para la entrega final:

```bash
agent/build-agent doctor --mode imaging
agent/build-agent build --mode imaging --output-dir dist/agent
agent/build-agent verify --output-dir dist/agent

pxe/build-pxe doctor
pxe/build-pxe build \
  --agent-dir dist/agent \
  --base-url https://pyfog.example/boot \
  --ipxe-efi /ruta/controlada/ipxe.efi \
  --output-dir dist/pxe
pxe/build-pxe verify --output-dir dist/pxe
uv run python -m scripts.release_check --require-artifacts
```

`make release-package` sólo acepta un checkout limpio y crea `dist/release/` con el tar de fuente
del `HEAD`, un manifiesto que liga versión/commit/tamaño/SHA-256 y un `SHA256SUMS`. Ese tar es el
artefacto del servidor: contiene el código y las instrucciones necesarias para construir la imagen
Docker; no contiene secretos ni `dist/`. El directorio de salida debe estar vacío y queda excluido
de Git.

El builder del agente registra versión, kernel, initramfs, herramientas, procedencia y SHA-256.
El builder PXE registra URL HTTPS, versión, timeout y SHA-256 de los archivos HTTP/TFTP. No se
deben modificar `manifest.json` o `SHA256SUMS` después de verificar; cualquier cambio exige
reconstruir el bundle. La imagen del servidor se construye con `deploy/Dockerfile`, cuyas versiones
de Python/uv están fijadas; `docker compose ... config` debe pasar para desarrollo y LAN.

## Instalación de humo

1. En una VM limpia, seguir [deploy/README.md](../deploy/README.md), aplicar migraciones y crear el
   administrador explícitamente.
2. Confirmar `/health/live` y `/health/ready`, iniciar sesión, registrar un equipo e importar un
   inventario de prueba.
3. Verificar el catálogo, una captura, una restauración y una clonación sólo con discos del
   laboratorio; revisar `Tareas`, `Estado` y `Auditoría`.
4. Crear/verificar un backup, reiniciar el servicio y comprobar que el catálogo y los temporales
   conservan el estado esperado.
5. Ejecutar `make e2e-plan` y, en un host con QEMU/OVMF, `make e2e`; conservar el informe generado
   por `lab/e2e.sh` sin tokens ni contraseñas.

## Matriz de la release

| Área | Comprobación | Resultado esperado |
| --- | --- | --- |
| Python | CI en 3.12 y 3.14; Ruff; mypy; pytest | Verde |
| Servidor | Compose dev/LAN, migraciones, healthchecks, backup/restore | Verde |
| Agente | `doctor`, build `imaging`, manifest y checksums | Verde en host Linux equipado |
| PXE | `doctor`, build, verificación y retorno al disco local | Verde con iPXE revisado |
| UEFI | Captura → restore, clone B y casos negativos | Informe de `make e2e` |
| Web | Teclado, 360/768/1280 px, errores y progreso | Checklist de `docs/accessibility.md` |
| Seguridad | `make audit`, revisión de secretos y procedencia | Verde sin credenciales reales |

## Alcance conocido

La release sólo admite manifiestos v1/v2 que describan Ubuntu x86_64, UEFI sin Secure Boot, GPT, ESP FAT32, raíz ext4 y swap
opcional, un disco por tarea y destino igual o mayor con sector lógico compatible. Usa un único
coordinador y SQLite para la LAN del MVP, unicast y administrador local. No incluye Windows, macOS,
BIOS/MBR, Secure Boot, LVM/RAID/cifrado, otros filesystems, varios discos, redimensionamiento,
multicast, plugins, dominio ni coordinación distribuida. El formato de imágenes es propio y no
promete compatibilidad con FOG o Clonezilla.

La procedencia/licencia de dependencias y binarios está en [docs/provenance.md](provenance.md).
Las limitaciones de recuperación de interrupciones y la rotación de secretos están en
[docs/backup-recovery.md](backup-recovery.md).
