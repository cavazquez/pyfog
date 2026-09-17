# ADR 0002: distribución de imágenes para restauraciones masivas

**Estado:** aceptado el 17 de septiembre de 2026.

**Issue:** [#50](https://github.com/cavazquez/pyfog/issues/50).

## Contexto

La transferencia unicast actual es suficiente para una operación del MVP, pero repetir un artefacto
de 20–80 GiB hacia unos 40 equipos aumenta linealmente el tráfico del servidor y del storage. El
plano de datos debe seguir separado del control: autenticación, selección de equipos, leases,
progreso y resultados permanecen en HTTPS; la distribución de piezas puede usar otro mecanismo.

Un artefacto `partclone.gz` es un stream. No es seguro descargar piezas directamente al disco que
se va a restaurar: antes hay que comprobar el manifiesto, cada pieza y el checksum completo, y recién
después ejecutar Partclone de forma secuencial. Para una imagen grande el receptor necesita staging
del tamaño real de los artefactos, no de la capacidad lógica del disco origen.

## Alternativas

| Alternativa | Ventaja | Riesgo o límite para el MVP |
| --- | --- | --- |
| HTTP Range desde un único servidor | Es simple y puede paralelizar una descarga | Sigue generando tráfico 40x; no es P2P |
| Torrent privado con `aria2c` y HTTP seed | Piezas verificables, reanudación, peers y fallback HTTP disponibles | Requiere tracker/seed controlado y un descriptor autenticado |
| Multicast confiable/NORM | Puede enviar una copia por segmento de red | Requiere IGMP snooping, NACK/FEC, reparación, control de congestión y operación de red |
| Árbol de relays | Reduce carga del seeder y no necesita multicast | Hay que coordinar relays, leases, cachés y fallos |
| S3/MinIO/Ceph multipart | Storage especializado y escalable | No elimina por sí mismo las descargas 40x; suma operación |
| NFS/pNFS | Backend compartido conocido | El acceso directo exige autorización, aislamiento y staging; montarlo detrás de FastAPI no cambia la ruta |
| HTTP/3/QUIC | Buen transporte unicast moderno | No resuelve la replicación entre 40 receptores |

La especificación de NORM sirve como referencia para diseños NACK-oriented y contempla reparación y
FEC, pero una implementación multicast propia agregaría un protocolo distribuido de alto riesgo. La
documentación de `aria2c` confirma soporte de torrent y web seed; por eso el experimento inicial puede
componerse con software existente sin implementar BitTorrent desde cero.

## Decisión

Elegimos un torrent privado por artefacto, `aria2c` como cliente del agente, tracker fijo dentro de
la LAN y HTTP seed HTTPS como fallback. El coordinador autoriza la sesión por HTTPS y entrega un
descriptor autenticado; no se habilitan DHT, NAT traversal ni peers fuera de la red administrada.
Cada agente mantiene su propio estado de descarga, lease, bitmap y resultado. La caída de un peer no
interrumpe a otros y una caída del seeder puede cubrirse con el HTTP seed o con peers que ya poseen
piezas.

La primera versión de la integración deberá:

1. Añadir al manifiesto v2 tamaño de pieza, hash por pieza o raíz Merkle, tamaño de artefacto,
   identificador de sesión y firma.
2. Descargar a staging preasignado o sparse, escribir por offset con `pwrite()` y persistir un
   bitmap para reanudar piezas fuera de orden.
3. Verificar pieza, manifiesto y checksum completo antes de Partclone.
4. Aplicar límites de peers, timeout, HTTP seed, espacio libre y eliminación segura de temporales.
5. Registrar métricas por sesión sin incluir tokens ni contenido de imagen en logs.

Multicast confiable queda como alternativa posterior. Sólo se elegirá si el laboratorio cableado
demuestra forwarding con IGMP snooping, aislamiento de otros segmentos, supresión de NACK, reparación
unicast, incorporación tardía y tolerancia a la caída del seeder.

## Experimento de validación

El laboratorio debe medir artefactos equivalentes a 20, 40 y 80 GiB con 1, 5 y 10 receptores
fallando, caída del seeder, incorporación tardía y reinicio con bitmap parcial. Se conservan tiempo
hasta que todos pueden restaurar, tráfico saliente del seeder, tráfico agregado de la LAN, throughput
de staging y disco, proporción de reparaciones y espacio adicional. La decisión se revisa con esos
datos antes de activar distribución masiva en producción.

## Consecuencias

El MVP conserva el camino unicast HTTPS y no obtiene todavía la reducción de tráfico: esta ADR fija la
dirección y los límites del experimento, no afirma que la distribución P2P ya esté implementada.
También deja explícito que una imagen comprimida no puede restaurarse con recepción parcial directa
sobre el disco destino.
