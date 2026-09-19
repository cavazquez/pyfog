# Coordinador activo/pasivo

`coordinator_leases` contiene una única fila lógica para el coordinador primario. Un nodo adquiere
la lease si está libre o vencida; al cambiar el dueño aumentan `term` y `fencing_token`. Renovar no
crea un token nuevo. Antes de cada mutación durable, el proceso debe llamar a
`pyfog.coordinator.assert_fenced`; un token viejo, un nodo pasivo o una lease vencida fallan cerrado.

Liberar una lease también incrementa el token para invalidar inmediatamente al proceso anterior.
El estado de health mantiene el contrato `integrated` cuando no se habilitó HA y expone
`active-passive`/`passive` cuando existe una fila de coordinación. La base de datos es el punto de
serialización; no se usa un lock de archivo local que quedaría partido entre nodos.
