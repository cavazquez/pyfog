# Roles y permisos administrativos

PyFog mantiene la identidad humana en una sesión web y autoriza cada acción con una política
explícita. La API de agentes continúa usando credenciales acotadas al equipo; no se reutiliza una
sesión web para inventario o transferencia de artefactos.

## Roles

| Rol | Puede hacer | No puede hacer |
| --- | --- | --- |
| `admin` | Todas las acciones, incluida la gestión de usuarios y borrado de imágenes | — |
| `operator` | Registrar y editar equipos, importar inventario, emitir tokens, aprobar PXE, capturar, restaurar, clonar y operar tareas | Borrar imágenes, gestionar usuarios, ver auditoría administrativa |
| `auditor` | Consultar equipos, inventarios, solicitudes, imágenes, tareas, estado, auditoría y backups | Mutar inventario, imágenes, restauraciones, borrados, tareas, usuarios o pairing |

La política se encuentra en `pyfog/rbac.py`. Los permisos desconocidos, los roles desconocidos y los
usuarios sin rol válido se rechazan. Las rutas mutantes vuelven a comprobar el permiso aunque el
botón no se muestre en la interfaz.

## Matriz de acciones

| Área | Permiso | Admin | Operador | Auditor |
| --- | --- | :---: | :---: | :---: |
| Inventario | `inventory.read` | ✓ | ✓ | ✓ |
| Inventario | `inventory.import` | ✓ | ✓ | — |
| Inventario | `inventory.token` | ✓ | ✓ | — |
| Imágenes | `images.read` | ✓ | ✓ | ✓ |
| Imágenes | `images.create`, `images.update`, `images.capture` | ✓ | ✓ | — |
| Imágenes | `images.delete` | ✓ | — | — |
| Restauración / clonación | `restore.execute`, `clone.execute` | ✓ | ✓ | — |
| Tareas | `tasks.read` | ✓ | ✓ | ✓ |
| Tareas | `tasks.cancel`, `tasks.reconcile` | ✓ | ✓ | — |
| Backups | `backup.read` | ✓ | ✓ | ✓ |
| Backups | `backup.create` | ✓ | ✓ | — |
| Backups | `backup.restore` | ✓ | — | — |
| Gobierno | `users.manage` | ✓ | — | — |

Las operaciones de backup que se ejecuten fuera de la web deben mapear su identidad de operador al
mismo permiso antes de invocar el procedimiento documentado en
[`backup-recovery.md`](backup-recovery.md). No se otorga acceso por el mero hecho de conocer una
ruta de archivo.

## Alta y migración

La migración RBAC agrega `users.role` y asigna `admin` a todas las cuentas MVP existentes. Un
administrador puede cambiar roles desde **Usuarios**. Para crear una cuenta nueva desde el host:

```bash
uv run python -m pyfog create-user --username operador --role operator
uv run python -m pyfog create-user --username auditor --role auditor
```

`create-admin` sigue creando una cuenta administradora para instalaciones nuevas. PyFog nunca
permite quitar el rol al último administrador.

## Auditoría

Cada decisión administrativa conserva actor, recurso, resultado, decisión (`allow`/`deny`) y
motivo. Una denegación genera `access.denied` antes de devolver HTTP 403; sólo se guardan el rol,
el permiso y la ruta o identificador interno. No se registran contraseñas, tokens, formularios,
manifiestos ni contenido de imágenes.
