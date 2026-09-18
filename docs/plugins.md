# Plugins, hooks y dominio

Los plugins opcionales se describen con `api_version: 1`, ejecutable, capacidades y permisos
explícitos. El runner (`pyfog.plugins.PluginRunner`) deshabilita el shell, limita tiempo/tamaño,
construye un entorno mínimo y correlaciona cada respuesta con `request_id`. El payload JSON no
puede contener passwords, tokens, credenciales ni claves. Si una integración necesita un secreto,
lo recibe una sola vez por stdin y nunca por argv, variable de entorno o evento de tarea.

Los hooks post-deploy (`pyfog.hooks`) son declarativos: tienen clave de idempotencia, timeout,
reintentos y rollback opcional. El ledger se marca después de cada éxito para que una reconexión no
repita un efecto lateral ya completado.

La integración de dominio (#72) no está embebida en el coordinador. `pyfog.domain` define la
capacidad `domain.join`, recibe una referencia a secreto y delega la unión/desunión a un plugin
administrado. El plugin debe soportar `domain.join`, `domain.leave` y `domain.rollback`, ser
idempotente por `idempotency_key` y devolver sólo JSON acotado. El servidor conserva la autorización
de la tarea y el plugin conserva sus permisos de red/AD explícitos.
