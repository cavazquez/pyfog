# Política de dependencias y secretos

PyFog no almacena credenciales reales, claves privadas ni tokens de acceso en el repositorio. Los
valores de prueba son deliberadamente falsos y se marcan de forma puntual para que el escáner los
distinga de una credencial accidental. El staging de imágenes queda fuera del repositorio y el agente
recibe el token del equipo mediante un archivo temporal provisionado fuera de iPXE.

## Controles automáticos

Cada push y pull request ejecuta estas comprobaciones en GitHub Actions:

```bash
uv run pip-audit --local --strict --progress-spinner off
git ls-files -z | xargs -0 uv run detect-secrets-hook --no-verify
```

`pip-audit` consulta su fuente pública de vulnerabilidades y bloquea la integración si encuentra una
vulnerabilidad conocida o no puede completar la auditoría. No recibe secretos de PyFog ni tiene
acceso a la LAN, a equipos PXE ni a discos físicos. `detect-secrets` revisa únicamente archivos
versionados; no se usa una línea base que pueda ocultar hallazgos nuevos.

Una excepción requiere el comentario en la misma línea `# pragma: allowlist secret`, sólo para un
valor determinista de prueba o un identificador generado que el escáner confunda con una clave. La
revisión que la introduce documenta la justificación en el pull request. No se permiten excepciones
para material de producción.

La cadena de confianza de Secure Boot y la custodia de sus claves están fijadas en la
[ADR 0003](adr/0003-secure-boot-trust-chain.md). Las claves privadas de release permanecen fuera
de Git, CI, argumentos, logs, imágenes y artefactos; sólo se publican certificados y fingerprints.

## Respuesta ante un hallazgo

Si aparece una credencial real, se revoca o rota de inmediato fuera del repositorio. Después se
elimina del historial que seguirá expuesto y se registra el incidente con el alcance y la rotación
realizada. Eliminar la línea de la rama actual no sustituye la revocación.

Las dependencias directas y transitivas se fijan en `pyproject.toml` y `uv.lock`. Dependabot abre
actualizaciones programadas en diciembre y las alertas de seguridad de GitHub permanecen habilitadas;
la programación anual no demora una actualización de seguridad urgente.
