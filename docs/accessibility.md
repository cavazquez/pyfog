# Accesibilidad y evidencia visual

Los flujos principales usan HTML semántico, labels asociados, foco visible, alertas anunciadas,
texto junto a los estados y tablas que se desplazan horizontalmente cuando el viewport es estrecho.
La navegación móvil se divide en dos filas de tres opciones; los formularios conservan los valores
recibidos cuando hay un error.

## Control automatizado

El control estático reproducible verifica labels, `aria-invalid`, mensajes de error, captions de
tablas, descripción del progreso y reglas de foco/responsive:

```bash
uv run pytest tests/test_accessibility.py -q
uv run ruff check pyfog tests/test_accessibility.py
```

Para una revisión de navegador, ejecutá la aplicación y usá una herramienta como axe DevTools sin
enviar los resultados a terceros. La revisión manual debe cubrir los casos vacíos, errores,
progreso, cancelación y confirmación de restauración/clonación.

## Matriz visual y de teclado

| Viewport | Pantallas | Evidencia a guardar en la revisión |
| --- | --- | --- |
| 360 px | login, registro, inventario, captura, restauración | no hay scroll horizontal de la página; tablas se desplazan dentro de su contenedor; controles táctiles tienen al menos 44 px |
| 768 px | las mismas pantallas | formulario y paneles se reorganizan sin cortar labels ni alertas; foco visible en orden natural |
| 1280 px | las mismas pantallas | navegación lateral, tablas completas y confirmación de destino legible |

Recorrido mínimo con teclado: `Tab` desde el enlace para saltar al contenido, completar login,
registrar equipo, cargar inventario, encolar captura, abrir restauración y clonación, recorrer imagen,
disco, hostname y confirmación, y volver a `Tareas`. Cada error debe anunciarse, mantener el valor
introducido y no depender sólo del color. En una operación activa, el estado y el progreso se
actualizan con texto y regiones `status`/`alert`.

La evidencia visual debe ser tomada contra una instancia de prueba sin datos reales y no debe
incluir tokens, contraseñas, certificados ni contenido de imágenes. El pipeline no inventa
capturas: el operador adjunta las imágenes tomadas en los tres tamaños cuando valida un release.
