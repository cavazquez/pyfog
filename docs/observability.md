# Observabilidad operativa

PyFog conserva eventos estructurados de cada tarea en `task_events`. El contrato no depende de un
SaaS y se puede exportar desde la ficha con `GET /tasks/{task_id}/events`, siempre con un usuario que
tenga `tasks.read`.

Cada evento contiene el `task_id`, el `attempt_id` cuando existe, la secuencia del agente, la fase,
los bytes procesados y el total declarado. Los eventos de progreso, error, cancelación, expiración y
finalización agregan duración en milisegundos y throughput entero en bytes por segundo. Los errores
usan un `failure_code` de un conjunto fijo (`manifest_invalid`, `artifact_integrity`, `storage_error`,
`lease_expired`, `cancelled`, entre otros); el motivo textual queda acotado para la pantalla y no se
usa como etiqueta de métrica.

Los tipos principales son `created`, `reserved`, `assigned`, `progress`, `cancellation_requested`,
`cancelled`, `error`, `reconciled`, `lease_expired` y `completed`. La reserva se registra cuando la
tarea toma el equipo y su lugar en la cola; la reconciliación sólo aparece después de la confirmación
explícita del operador.

## Salud agregada

`GET /health/metrics` es un endpoint portable para probes y colectores. Devuelve una ventana fija de
24 horas con conteos por estado, tipos de evento y códigos de fallo, además de bytes activos,
duración promedio y throughput promedio. No devuelve nombres, UUIDs de tareas, mensajes, rutas,
inventarios, artefactos, tokens ni contraseñas. `/health/live` y `/health/ready` conservan su contrato
de liveness/readiness.

Los dos identificadores de correlación (`task_id` y `attempt_id`) sólo aparecen en el exportador
autenticado por tarea. `sequence` permite unir un reintento del agente con el evento persistido sin
duplicarlo; los mensajes se limitan y se redactan para patrones comunes de Bearer y credenciales.

## Retención y privacidad

Los eventos viven junto con la tarea y se eliminan por cascada cuando se elimina su tarea. El MVP no
purga automáticamente historial operativo: la instalación debe definir una retención local (30 días
es una recomendación inicial), hacer backup antes de purgar y conservar únicamente lo necesario para
diagnóstico. Los backups siguen sujetos a la política de [backup y recuperación](backup-recovery.md).

No se deben enviar secretos, contenido de manifiestos ni datos de discos en `message`. Las métricas
agregadas no incluyen mensajes y el endpoint detallado requiere RBAC; revisar ambos contratos es
parte del cambio de formato.
