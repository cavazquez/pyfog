# Credenciales rotables de agentes

Cada emisión crea un valor aleatorio nuevo y guarda únicamente su SHA-256 en `agent_credentials`.
La credencial vigente se conserva en la ficha del equipo para compatibilidad con el agente MVP, pero
la API consulta la tabla de generaciones y nunca necesita conocer el valor anterior.

## Rotación

Al emitir una credencial nueva, las generaciones no revocadas anteriores reciben una ventana de
gracia acotada (`token_rotation_grace_seconds`, cinco minutos por defecto). La ventana nunca se
extiende por una rotación posterior: un agente desconectado puede volver a conectarse durante la
gracia, pero no se convierte en una credencial indefinida. La nueva credencial se muestra una sola
vez en la web y el puntero legado del equipo siempre queda en la generación más reciente.

Las rotaciones concurrentes pueden dejar más de una generación dentro de su propia gracia; ninguna
reescribe el hash de otra y todas se identifican por el hash almacenado. Esto permite que un agente
que estaba offline termine su reintento sin invalidar una credencial más reciente.

## Revocación y tareas

Revocar un equipo marca todas sus generaciones como revocadas y elimina el puntero legado en una
misma transacción. La comprobación se hace en cada `claim`, inventario, heartbeat, progreso,
fragmento y resultado. Las capacidades de tareas nuevas quedan vinculadas a la generación que las
emitió; por eso revocar también detiene inmediatamente transferencias ya reclamadas.

El replay de una capacidad sigue protegido por la secuencia monotónica de la tarea y por la lease.
Una capacidad revocada recibe `401` sin revelar si la generación existía, y una lease vencida sigue
requiriendo intervención explícita.

## Migración y recuperación

La migración convierte el hash y vencimiento únicos del MVP en una primera generación. Las rutas y
los agentes MVP no cambian: siguen enviando `Authorization: Bearer` a los mismos endpoints. Las
credenciales temporales de pairing continúan siendo independientes y de un solo uso.

Los backups contienen hashes, no valores Bearer. Al restaurar una instalación, se invalidan las
sesiones y todas las generaciones de agentes del backup; antes de volver a habilitar equipos se debe
emitir una credencial nueva desde la ficha correspondiente. Así un archivo restaurado no reabre el
acceso de un agente que estaba desconectado durante el incidente.

Los valores Bearer no aparecen en URLs, argumentos, logs, informes, auditoría ni manifiestos. El
archivo temporal de arranque sigue siendo el único lugar donde el agente recibe el valor en claro.
