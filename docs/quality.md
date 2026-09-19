# Calidad y lint

Ruff es obligatorio para el código versionado. `make lint` ejecuta el perfil obligatorio y
`ruff format --check` verifica que el formato no se desvíe. La CI publica los hallazgos como
anotaciones de GitHub para que una violación quede asociada a su línea.

El perfil obligatorio combina reglas de sintaxis, imports, seguridad, pathlib, fechas, tests y
calidad de bajo ruido con estas familias adicionales:

- `ANN204`: los constructores declaran explícitamente `-> None`.
- `ARG`: no se dejan argumentos sin usar; los parámetros de fixtures pytest se exceptúan porque
  su nombre declara una dependencia del test.
- `BLE`: no se capturan excepciones ciegamente.
- `ERA`, `G`, `LOG`, `PERF` y `RSE`: no se conserva código comentado, logging ambiguo, patrones
  de rendimiento evitables ni excepciones sin encadenamiento seguro.

Las excepciones quedan limitadas a tests y a los falsos positivos documentados en
`pyproject.toml`; no hay una exclusión global del perfil.

## Deuda de reglas de alto ruido

`make lint-advisory` informa, sin bloquear la entrega, el estado de `ANN401`, `EM`, `PLR` y
`TRY`. Esas reglas requieren una migración separada porque actualmente señalan decisiones
legítimas de compatibilidad, validación y mensajes de error en varios módulos. El objetivo es
reducir ese contador por tandas y convertir cada familia en obligatoria sólo cuando no necesite
una excepción masiva.

Para validar localmente:

```bash
make lint
make lint-advisory
uv run ruff format --check .
```
