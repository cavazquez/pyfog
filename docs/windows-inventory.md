# Contrato de inventario Windows

PyFog usa el mismo documento de inventario versionado para Windows y Linux (`schema_version: 1`).
La diferencia de plataforma se expresa en `os.id: "windows"`; no se infiere desde el hostname.
El validador local está en `pyfog.platform_inventory.validate_windows_inventory` y el fixture
sintético está en `tests/fixtures/platform/windows-inventory.json`.

El agente de referencia es `agent/windows/pyfog-agent.ps1`. Sólo consulta CIM/WMI y adaptadores
de red, emite hostname, versión, arquitectura, firmware, memoria, discos, red e identidad, y
envía por `POST /api/v1/hosts/{host_id}/inventory` con la credencial rotada del host. El token se
lee desde `PYFOG_INVENTORY_TOKEN`, se usa únicamente en el header Bearer y nunca se serializa ni
se pasa como argumento a una herramienta.

Los discos deben tener `stable_id` `serial:<valor>` o `wwn:<valor>`; no se acepta el orden de
`PhysicalDriveN` como identidad. Un campo no autorizado se representa en `warnings` y no se
rellena inventando datos. Si faltan hostname, versión, arquitectura, firmware, una MAC válida o
una identidad de disco, el documento se rechaza antes del transporte.

La instalación puede ejecutarse como tarea programada con la política de mínimo privilegio que
permita leer CIM/WMI y red. Para actualizar, se reemplaza el script firmado y se valida un fixture
antes de habilitar la tarea nueva. Para revocar, se revoca la credencial del host en PyFog; no se
revoca borrando el archivo local. El contrato no captura ni restaura Windows.
