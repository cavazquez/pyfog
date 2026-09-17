# Changelog

## 0.1.0 — 2026-09-17

Primera entrega verificable del MVP Linux de PyFog.

### Incluye

- Registro manual, descubrimiento PXE aprobado, inventario JSON y API con tokens revocables.
- Catálogo de imágenes, captura Partclone de solo lectura y publicación con manifiesto SHA-256.
- Restauración GPT/ESP/ext4/swap opcional con validación del destino y arranque UEFI fallback.
- Clonación con hostname, machine-id, claves SSH y red independientes del origen.
- Tareas persistentes con leases, progreso, cancelación cooperativa, intervención y reconciliación.
- Eliminación segura, auditoría, healthchecks, backup/recuperación, Compose LAN y guía operativa.

### Validación y límites

La matriz admitida es Ubuntu x86_64 con UEFI sin Secure Boot, GPT, ESP FAT32, raíz ext4 y swap
opcional, un disco por tarea y destinos de igual o mayor capacidad con sector lógico compatible.
La transferencia es unicast y el despliegue usa un único coordinador FastAPI con SQLite. Windows,
macOS, BIOS/MBR, Secure Boot, LVM/RAID/cifrado, otros filesystems, varios discos, multicast y
coordinación distribuida quedan fuera de esta versión. La release no afirma compatibilidad binaria
con FOG ni Clonezilla.

Los artefactos del agente y del perfil PXE se deben construir, verificar y publicar junto con sus
`manifest.json` y `SHA256SUMS`; el procedimiento está en
[`docs/release-0.1.0.md`](docs/release-0.1.0.md).
