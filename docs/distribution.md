# Distribución masiva y reparación

Los issues #69, #77 y #78 comparten una frontera: HTTPS autoriza la sesión y cada tarea, mientras
que el plano de datos sólo mueve piezas verificables. La implementación local está en
`pyfog.transfer`, `pyfog.relay` y `pyfog.distribution`.

Cada artefacto usa bloques con offset, tamaño y SHA-256. Un relay publica por digest después de
verificar el archivo completo, sirve rangos acotados y no elimina una entrada mientras tiene una
lease activa. El receptor multicast mantiene un bitmap por host, genera NACKs sólo para sus piezas
faltantes y acepta reparación unicast; ningún ACK individual bloquea a los demás receptores.

La decisión de estrategia se calcula con mediciones, no por configuración implícita:

- `p2p` requiere tracker privado, peers externos deshabilitados, reanudación y fallos aislados;
- `multicast` requiere IGMP snooping, ACL aislada, endpoint de reparación, incorporación tardía,
  failover del seeder y una tasa de reparación menor o igual a 15%;
- `relay` requiere al menos un relay y un benchmark completo;
- sin evidencia se conserva `unicast` HTTPS.

El informe se puede generar sin abrir sockets:

```bash
python3 -m scripts.benchmark_distribution benchmark.json
```

El JSON de entrada contiene `measurements` y, opcionalmente, `topology`. El resultado es una
decisión auditable con fallback y cantidad de corridas. Para habilitar multicast en un laboratorio
hay que aportar las mediciones de 20/40/80 GiB, fallos 1/5/10, caída del seeder, incorporación
tardía y reinicio con bitmap parcial descritas en ADR-0002; un archivo de configuración no alcanza.
