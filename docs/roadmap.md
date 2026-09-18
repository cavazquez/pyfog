# Roadmap de PyFog MVP Linux

<!-- pyfog-backlog:ROADMAP -->

## Estado

PyFog v0.1.0 queda completo como MVP funcional para una LAN administrada: registra
equipos Linux, obtiene inventario, captura imágenes, restaura el equipo de origen y
clona otro equipo regenerando su identidad. Las entregas M1 a M5 y sus issues de
trabajo están implementados.

## Alcance y decisiones

- Solo Linux x86_64, UEFI sin Secure Boot, GPT, raíz ext4, ESP FAT32, swap opcional y un disco por tarea.
- Una LAN administrada, un servidor, un administrador local y transferencia unicast.
- Restaurar conserva la identidad del origen; clonar regenera la identidad del destino.
- Las imágenes no prometen compatibilidad binaria con FOG.
- Multicast y distribución masiva quedan fuera de esta versión; la decisión está documentada en [ADR-0002](adr/0002-distribucion-masiva.md).

## Issues completados

### M1 · Registro e inventario Linux

- [x] #2 Arquitectura y contrato de compatibilidad.
- [x] #3 Aplicación Python y persistencia.
- [x] #5 Controles de calidad en cada pull request.
- [x] #6 Acceso web mediante administrador local.
- [x] #8 Navegación y componentes responsive.
- [x] #9 Registro manual por MAC.
- [x] #10 Formato versionado del inventario.
- [x] #11 Recolección Linux sin dependencias Python.
- [x] #12 Importación de inventario JSON.
- [x] #13 Credenciales revocables del agente.
- [x] #14 Envío autenticado de inventario.
- [x] #15 Consulta y búsqueda de equipos.
- [x] #16 Historial de inventarios.
- [x] #48 Dependencias fijadas y revisión anual.

### M2 · Arranque PXE y registro

- [x] #7 HTTPS para web y agente.
- [x] #17 Laboratorio UEFI reproducible.
- [x] #18 Entorno Linux de inventario e imágenes por red.
- [x] #19 PXE/UEFI con retorno al disco local.
- [x] #20 Descubrimiento y aprobación desde la web.

### M3 · Captura de imágenes

- [x] #21 Manifiesto versionado de imagen.
- [x] #22 Catálogo web de imágenes.
- [x] #23 Almacenamiento con publicación atómica.
- [x] #24 Persistencia y reserva de tareas.
- [x] #25 Concesiones temporales al agente.
- [x] #26 Transferencia HTTPS de artefactos.
- [x] #27 Validación del disco Linux.
- [x] #28 Captura y publicación verificable.
- [x] #29 Solicitud de captura desde la ficha del equipo.
- [x] #30 Progreso e historial de tareas.

### M4 · Restauración y clonación

- [x] #31 Validación del disco destino.
- [x] #32 Restauración del layout y particiones.
- [x] #33 Arranque UEFI restaurado.
- [x] #34 Solicitud con confirmación del destino.
- [x] #35 Identidad Linux independiente para clones.
- [x] #36 Clonación sobre otro equipo registrado.
- [x] #37 Cancelación de tareas pendientes o activas.
- [x] #38 Reconciliación tras pérdida del agente o reinicio.

### M5 · Operación y entrega

- [x] #4 Entorno de desarrollo con un comando.
- [x] #39 Eliminación segura de imágenes.
- [x] #40 Auditoría administrativa y destructiva.
- [x] #41 Estado operativo y almacenamiento.
- [x] #42 Respaldo y recuperación del servidor.
- [x] #43 Instalación del servidor para una LAN.
- [x] #44 Guía del recorrido completo.
- [x] #45 Accesibilidad y responsive.
- [x] #46 Contrato E2E reproducible y runner de laboratorio QEMU/UEFI.
- [x] #47 Entrega verificable v0.1.0.

## Post-MVP · compatibilidad y fixtures

- [x] #51 Contrato de capacidades de imagen v2, adaptación v1 y rechazo temprano.
- [x] #74 Matriz declarativa y fixtures QCOW2 deterministas para perfiles soportados y negativos.
- [x] #63 ADR de cadena de confianza, propietarios, rotación y negativos de Secure Boot.
- [x] #66 Roles admin/operator/auditor, matriz deny-by-default, gestión web y auditoría de denegaciones.
- [x] #67 Rotación con gracia, revocación inmediata, binding de tareas y recuperación offline de credenciales de agentes.

## Evidencia de validación

- 34 pruebas dirigidas pasan, incluidas las de contrato E2E, release, restauración,
  tareas, manifiestos y accesibilidad.
- Ruff, formato Ruff, mypy, compilación Python, sintaxis Bash y `git diff --check`
  pasan.
- `make release-check`, `make e2e-plan` y `make release-package` pasan.
- El paquete fuente reproducible se genera en `dist/release/` con `manifest.json` y
  `SHA256SUMS`, asociado al commit `c1eb3f8c58eb4f91080f91c7ed68173541438ac4`.
- `lab/e2e.sh run` registra un informe y no inicia una prueba incompleta si faltan
  dependencias del host. En un host Ubuntu 26.04 equipado, el 18/09/2026 pasó el
  contrato de 31 pruebas y arrancó `source` y `target` con Linux por UEFI; ambas
  salidas seriales llegaron a `cloud-init.target` y al prompt de login. El informe
  quedó en `.e2e/run.*` y las VMs se detuvieron limpiamente.
- `make compatibility-check` genera ocho QCOW2 descartables y verifica su hash, tamaño,
  capacidades y rechazo del agente sin usar discos físicos ni credenciales.
- `uv run pytest tests/test_rbac.py` verifica la matriz de permisos, la migración de cuentas MVP,
  la denegación de acciones y la auditoría con actor, recurso, decisión y motivo.
- `uv run pytest tests/test_agent_credentials.py` y las pruebas de seguridad/tareas verifican
  rotaciones encadenadas, gracia acotada, replay, revocación de transferencias y recuperación sin
  credenciales activas desde backup.

## Limitaciones conocidas de v0.1.0

Windows, macOS, BIOS/MBR, Secure Boot, LVM/RAID/cifrado, otros sistemas de archivos,
reducción automática de disco, captura en caliente, varios discos por tarea,
multicast, plugins y administración de software/dominio quedan fuera del MVP.
También queda fuera la publicación automática de un tag o release externo.

La documentación de entrega, matriz y limitaciones está en
[`docs/release-0.1.0.md`](release-0.1.0.md); la procedencia y licencias, en
[`docs/provenance.md`](provenance.md).
