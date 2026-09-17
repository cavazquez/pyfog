# Investigación: distribución multicast/P2P para restauraciones masivas

## Objetivo

Investigar y seleccionar el plano de datos para restaurar una misma imagen Linux en aproximadamente
40 equipos simultáneamente, sin que el coordinador, HTTPS ni el storage central sean el cuello de
botella.

## Contexto actual

- PyFog usa HTTPS para el canal del agente, las capacidades de tarea y el control de la operación.
- HTTPS no reemplaza a TCP: actualmente es HTTP/TLS sobre TCP. UDP sólo sería viable mediante un
  protocolo confiable como QUIC o reliable multicast.
- La captura actual sube artefactos por fragmentos HTTP; la restauración y clonación todavía no están
  implementadas.
- Una imagen puede corresponder a un disco de hasta aproximadamente 80 GiB, aunque los artefactos
  Partclone comprimidos pueden ocupar bastante menos.
- Los artefactos actuales son streams `partclone.gz`, publicados con manifiesto y SHA-256.

## Alternativas a investigar

### Multicast confiable

Evaluar multicast IP para enviar una sola copia desde el seeder y replicarla en los switches.

Debe contemplar:

- FEC y/o NACK con supresión de feedback.
- Reparación unicast para paquetes o piezas faltantes.
- IGMP snooping y requisitos de red.
- Reanudación y clientes que se incorporan tarde.
- Seeder secundario o mecanismo de reparación si el seeder falla.
- No esperar ACK de los 40 equipos para continuar: la finalización debe ser por receptor.

Una caída de un receptor no debe interrumpir a los demás. Si un receptor pierde un paquete, detecta
el hueco por número de secuencia y solicita sólo la reparación necesaria. Si el seeder falla, la
sesión puede continuar únicamente si existe otro seeder, una cache de piezas entre peers o un
backend de reparación.

### P2P tipo BitTorrent usando aria2c

Investigar generar un torrent o metadatos equivalentes para cada imagen/artefacto y usar `aria2c`
como cliente en el agente.

Para una LAN controlada no sería necesario implementar DHT, NAT traversal ni el protocolo BitTorrent
desde cero. Se podría usar:

- tracker fijo o descubrimiento local;
- HTTP seed como fallback;
- límite de peers por agente;
- piezas verificables;
- HTTPS sólo para autorizar la sesión y entregar el descriptor.

Distinguir entre:

- `aria2c` descargando por HTTP Range desde un único servidor, que mejora el paralelismo de una
  descarga individual pero no elimina el tráfico 40x;
- `aria2c` usando torrent y compartiendo piezas entre agentes, que sí reduce la carga del seeder.

### Otras alternativas

Comparar con:

- árbol de distribución o relays;
- almacenamiento S3-compatible/MinIO/Ceph con multipart upload;
- NFSv4.1 como backend compartido;
- pNFS sólo si el storage tiene varios data servers;
- HTTP/3/QUIC si las mediciones justifican su uso.

NFS montado en FastAPI no elimina la ruta agente → HTTPS → aplicación → NFS. NFS directo al agente
requeriría resolver autorización, aislamiento por tarea, staging y publicación atómica.

## Recepción fuera de orden y una imagen de 80 GiB

P2P no guarda toda la imagen en memoria ni exige recibirla en orden. El receptor debe:

1. Crear un archivo de staging preasignado o sparse del tamaño del artefacto.
2. Definir piezas con índice y offset: `offset = piece_index * piece_size`.
3. Verificar el hash de cada pieza.
4. Escribirla con `pwrite()` en su offset, aunque llegue fuera de orden.
5. Mantener un bitmap o sidecar de piezas recibidas para reanudar.
6. Al completar todas las piezas, verificar el hash completo del artefacto.
7. Validar el manifiesto y recién entonces ejecutar Partclone contra el disco destino.

La capacidad necesaria en staging es el tamaño real de los artefactos, no necesariamente la
capacidad del disco origen. Sin un segundo medio, storage remoto o espacio libre que no vaya a ser
sobrescrito, no es posible guardar una imagen completa y luego restaurarla sobre el mismo disco bajo
el formato actual.

No se deben escribir directamente las piezas recibidas sobre el disco destino: los artefactos
comprimidos de Partclone son streams y la restauración debe realizarse secuencialmente después de
validar la imagen. Si no hay espacio para staging, investigar un formato de chunks independientes o
seekable que permita una restauración por bloques, aceptando una semántica de fallo distinta.

Agregar al manifiesto v2:

- tamaño de pieza;
- hash por pieza o raíz Merkle;
- tamaño de cada artefacto;
- identificador de sesión;
- firma o autenticación del descriptor.

## Fallos y reanudación

La pérdida de un paquete o la caída de una máquina receptora no debe cancelar las restauraciones de
los demás hosts. Cada receptor mantiene su propio estado, bitmap, lease y resultado.

El protocolo debe probar:

- pérdida aislada de paquetes en un receptor;
- pérdida de paquetes en varios receptores;
- pérdida de un enlace del switch;
- receptor que queda offline y vuelve a unirse;
- caída del seeder durante la transferencia;
- caída de un receptor durante staging;
- caída de un receptor durante la restauración del disco.

Un receptor que falla durante staging debe poder reanudar. Si falla durante la escritura del disco
destino, sólo su tarea debe quedar en `intervention_required`; no se deben cancelar hosts que ya
restauran correctamente.

## Modelo de control propuesto

Separar:

- `DeploymentSession`: operación grupal, imagen, estrategia de distribución, seeder y estado global.
- `Task` por host: autorización, lease, progreso, verificación y resultado individual.

Flujo:

```text
HTTPS: aprobar hosts, entregar descriptor/capacidad y coordinar la sesión
Data plane: multicast confiable o swarm P2P
Cada agente: staging → hashes → validación → restore local
HTTPS: resultado independiente por host
```

## Experimentos

Medir con imágenes de 20, 40 y 80 GiB:

- tiempo total hasta que todos los hosts pueden restaurar;
- tráfico saliente del seeder;
- tráfico agregado de la LAN;
- throughput de staging y del disco destino;
- porcentaje de reparación/retransmisión;
- comportamiento con 1, 5 y 10 receptores fallando;
- caída del seeder durante la transferencia;
- incorporación tardía de un receptor;
- reinicio de un receptor con bitmap parcial;
- espacio adicional requerido por staging.

## Criterio de decisión

Preferir la opción que:

1. mantenga HTTPS para control y autorización;
2. permita verificar la imagen antes de modificar el disco;
3. tolere fallos individuales sin abortar la sesión completa;
4. permita reanudar una imagen grande sin reiniciarla desde cero;
5. reduzca el tráfico y la carga del seeder;
6. pueda probarse en el laboratorio cableado previsto para el MVP.

Resultado esperado: ADR o decisión técnica con una implementación inicial recomendada, probablemente
`aria2c + torrent/HTTP seed` para P2P o multicast confiable con reparación unicast si la red soporta
multicast de forma consistente.

## Decisión

La primera implementación experimental será P2P con un torrent privado por artefacto, `aria2c` en
el agente, tracker fijo dentro de la LAN y HTTP seed autenticado como fallback. El coordinador
seguirá usando HTTPS para autorizar la sesión, entregar el descriptor firmado y publicar el resultado
independiente de cada equipo. No se implementará BitTorrent, DHT, NAT traversal ni un protocolo
multicast propio dentro del MVP.

La elección se basa en que `aria2c` ya soporta torrent, tracker, peers y web seed, mientras que una
solución multicast confiable exige diseñar y operar además secuenciación, NACK/FEC, supresión de
feedback, reparación y control de congestión. El protocolo NORM documenta esas piezas y sirve como
referencia si el laboratorio demuestra que la topología y el switching administrado las soportan,
pero no es una razón para introducir un transporte UDP propio ahora.

El descriptor de imagen v2 deberá incluir el tamaño de pieza, hashes por pieza o una raíz Merkle,
el tamaño de cada artefacto, la sesión y una firma. El agente descargará las piezas a un archivo de
staging preasignado con `pwrite()` y un bitmap persistente; verificará el hash de pieza y el hash
completo antes de invocar Partclone. Las piezas nunca se escribirán directamente sobre el disco de
destino. La caída de un peer sólo afecta su tarea; una caída del tracker o seeder se cubre mediante
el HTTP seed y peers que ya tengan piezas.

La decisión se revisará cuando existan mediciones reproducibles en el laboratorio con 20, 40 y 80
GiB, 1/5/10 receptores fallando, incorporación tardía, reinicio con bitmap parcial y caída del
seeder. Se conservarán tiempo hasta que todos puedan restaurar, tráfico del seeder, tráfico total,
reparaciones, throughput de staging y espacio adicional. Si multicast no tiene IGMP snooping,
aislamiento y reparación operables en la LAN, se descarta para esta versión.

Referencias técnicas: [aria2c BitTorrent y web seed](https://aria2.github.io/manual/en/html/aria2c.html),
[NORM](https://www.rfc-editor.org/rfc/rfc5740.html), [NACK multicast](https://www.rfc-editor.org/rfc/rfc5401.html)
y [consideraciones de IGMP snooping](https://www.rfc-editor.org/rfc/rfc4541.html).
