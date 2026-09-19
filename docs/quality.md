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

La suite usa `TestClient` y mantiene el lifespan real de la aplicación. En sandboxes locales que
bloquean el `send()` del socket interno de `asyncio`, `tests/conftest.py` detecta el `EPERM` y aplica
un wake-up equivalente con `os.write`; en CI y en un entorno normal no se modifica el event loop.

## Cobertura y límites de CI

`make test-coverage` mantiene los umbrales agregados de 70% para `pyfog` y 40% para `scripts`, con
cobertura de ramas. Además verifica un piso de 60% para los módulos críticos de API, web, layouts,
dominio y claves, y un piso de 35% para los scripts operativos principales. Los wrappers de CLI que
requieren integración de laboratorio no se cuentan como cobertura unitaria artificial; sus contratos
se validan en los checks específicos.

La CI corta la suite de tests a los 120 segundos, la cobertura a los 180 y las migraciones a los 60;
cada job también tiene un límite total de 10 a 20 minutos. Pytest emite un dump de threads a los 30
segundos para que un timeout conserve diagnóstico útil.
