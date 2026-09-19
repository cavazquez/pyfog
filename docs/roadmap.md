# Roadmap de PyFog MVP Linux

<!-- pyfog-backlog:ROADMAP -->

## Estado

PyFog v0.1.0 queda completo como MVP funcional para una LAN administrada: registra
equipos, obtiene inventario, captura imágenes, restaura el equipo de origen y clona
otro equipo regenerando su identidad. Las entregas M1 a M5 están implementadas.
La ampliación de compatibilidad, transferencia y operación de los issues abiertos
también quedó incorporada como rutas ejecutables, contratos versionados, fixtures y
pruebas; los perfiles que dependen de herramientas del initramfs se habilitan por
capacidades anunciadas por el agente.

## Alcance y decisiones

- El imaging sigue siendo Linux x86_64 y no incluye Windows/macOS: Windows tiene agente de
  inventario y macOS conserva una evaluación no destructiva. Secure Boot sigue fuera de los
  perfiles de imagen.
- La matriz de imagen admite UEFI/GPT y BIOS/MBR, ext4, XFS, Btrfs, multidisco, expansión ext4,
  LVM lineal, RAID1, LUKS2 con proveedor externo y captura en caliente cuando el agente anuncia
  las herramientas correspondientes.
- Una LAN administrada conserva HTTPS como plano de control y unicast como fallback seguro;
  relay, reparación multicast y benchmark son contratos opt-in y no se activan sin evidencia de
  laboratorio.
- Restaurar conserva la identidad del origen; clonar regenera la identidad del destino.
- Las imágenes no prometen compatibilidad binaria con FOG.
- La distribución masiva queda en contratos opt-in y fallback unicast hasta completar mediciones;
  la decisión está documentada en [ADR-0002](adr/0002-distribucion-masiva.md).

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
- [x] #64 Firma Authenticode reproducible, verificación de firmas y smoke OVMF Secure Boot.
- [x] #66 Roles admin/operator/auditor, matriz deny-by-default, gestión web y auditoría de denegaciones.
- [x] #67 Rotación con gracia, revocación inmediata, binding de tareas y recuperación offline de credenciales de agentes.
- [x] #68 Métricas agregadas, eventos estructurados de tareas, correlación autenticada y health exporter sin secretos.
- [x] #62 Arranque BIOS con MBR y reinstalación/verificación de GRUB BIOS.

## Issues extendidos implementados

- [x] #52 Captura, selección y restauración de varios discos por identidad estable.
- [x] #53 Expansión segura de un destino mayor para raíz ext4, con preflight de herramientas.
- [x] #54 Reducción offline verificable mediante un reductor declarativo, sin tocar un disco montado.
- [x] #55 Captura en caliente con `fsfreeze`, capability-gating y thaw garantizado.
- [x] #56 Transferencias por bloques verificables, rangos y reanudación sin aceptar escritura parcial.
- [x] #57 Captura/restauración de LVM lineal con metadata no secreta y validación previa.
- [x] #58 LUKS2 con metadata pública y proveedor de clave externo por plugin.
- [x] #59 Captura/restauración de RAID1 mdadm, orden de miembros e identidad estable.
- [x] #60 XFS con herramientas anunciadas por el agente.
- [x] #61 Btrfs con subvolumen declarado y herramientas anunciadas por el agente.
- [x] #65 Contrato de inventario Windows, fixture y agente PowerShell de sólo lectura.
- [x] #69 Relay cache con lease y rangos, [x] #70 plugins versionados y [x] #71 hooks idempotentes.
- [x] #72 Integración de dominio mediante plugin con referencia de secreto fuera del payload.
- [x] #73 Coordinador activo/pasivo con lease, fencing, migración y fail-closed en mutaciones.
- [x] #75 Agente Windows reproducible por fixture y check sintáctico en runner Windows.
- [x] #76 Evaluación documentada de macOS sin prometer imaging ni desbloqueo de FileVault.
- [x] #77 Benchmark auditable para decidir unicast, relay, P2P o multicast con fallback explícito.
- [x] #78 Estado multicast con bitmap, NACK acotado y reparación unicast; transporte real desactivado.

## Evidencia de validación

- 188 pruebas pasan, incluidas las de contrato E2E, release, restauración, tareas, manifiestos,
  plugins, distribución y plataformas.
- Ruff, formato Ruff, mypy estricto sobre `pyfog`/`scripts`, compilación Python, sintaxis Bash y
  `git diff --check` pasan.
- La cobertura con ramas es 70% para `pyfog` y 47% para `scripts` (60% combinada); CI exige
  los umbrales por componente y mantiene visible la brecha del agente CLI.
- `make release-check` y `make e2e-plan` pasan; `make release-package` queda listo para
  ejecutarse sobre un commit limpio, como exige el empaquetador reproducible.
- El paquete fuente reproducible se genera en `dist/release/` con `manifest.json` y
  `SHA256SUMS`; el hash de commit se toma del checkout que se está entregando.
- `lab/e2e.sh run` registra un informe y no inicia una prueba incompleta si faltan
  dependencias del host. En un host Ubuntu 26.04 equipado, el 18/09/2026 pasó el
  contrato de 31 pruebas y arrancó `source` y `target` con Linux por UEFI; ambas
  salidas seriales llegaron a `cloud-init.target` y al prompt de login. El informe
  quedó en `.e2e/run.*` y las VMs se detuvieron limpiamente.
- `./lab/e2e.sh run --secure-boot` pasó el contrato y arrancó `source` y `target` con
  `OVMF_CODE_4M.secboot.fd` y `OVMF_VARS_4M.ms.fd`; el informe registró
  `secure_boot=enabled` y `signature_status=verified-by-ovmf`.
- `./lab/e2e.sh run --bios` pasó el contrato y ejecutó un MBR descartable con SeaBIOS; el
  sector 0 emitió la marca serial `PYFOG_BIOS_MBR_OK` y el informe conservó sus logs.
- `make compatibility-check` genera ocho QCOW2 descartables y verifica su hash, tamaño y
  capacidades sin usar discos físicos ni credenciales; los perfiles extendidos tienen fixtures JSON
  y pruebas dirigidas del agente.
- `uv run pytest tests/test_rbac.py` verifica la matriz de permisos, la migración de cuentas MVP,
  la denegación de acciones y la auditoría con actor, recurso, decisión y motivo.
- `uv run pytest tests/test_agent_credentials.py` y las pruebas de seguridad/tareas verifican
  rotaciones encadenadas, gracia acotada, replay, revocación de transferencias y recuperación sin
  credenciales activas desde backup.
- `uv run pytest tests/test_observability.py` verifica el esquema de eventos, métricas de duración/
  throughput, códigos de fallo, redacción de mensajes y el endpoint de salud agregado.

## Limitaciones remanentes

El agente Windows sólo inventaría; no captura ni restaura Windows. macOS queda limitado a la
evaluación de inventario. Secure Boot, cifrado de volumen combinado con LVM/RAID y reducción de
disco siguen fuera de las rutas de imaging. La implementación de distribución masiva mantiene
unicast HTTPS por defecto: los módulos de relay/multicast y el benchmark no abren sockets ni
habilitan peers automáticamente; requieren integración de transporte y evidencia de red del
laboratorio. Tampoco se publica automáticamente un tag o release externo.

La documentación de entrega, matriz y limitaciones está en
[`docs/release-0.1.0.md`](release-0.1.0.md); la procedencia y licencias, en
[`docs/provenance.md`](provenance.md).
