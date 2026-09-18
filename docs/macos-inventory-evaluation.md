# Evaluación de inventario macOS

La decisión de esta evaluación es **postergar** el agente macOS y limitar el alcance a inventario
no destructivo. El fixture sintético `tests/fixtures/platform/macos-inventory.json` demuestra la
forma del contrato (`os.id: "macos"`) sin requerir un equipo Apple ni `system_profiler` durante CI.

El equivalente de hostname, versión, arquitectura, firmware, discos, red e identidad se obtiene
con APIs de sólo lectura (`sw_vers`, `uname`, `system_profiler SPHardwareDataType`,
`system_profiler SPStorageDataType` e interfaces de red). La identidad debe derivarse de un
serial/UUID persistente; nunca del nombre `diskN`. Los permisos de Full Disk Access pueden cambiar
qué campos están disponibles y se reportan como advertencias, no como valores inventados.

El transporte y la revocación son los mismos del agente Linux: HTTPS, credencial hash-only en el
servidor y revocación server-side. No se promete imaging, modificación de discos, APFS snapshot,
FileVault unlock ni restauración macOS. La matriz pública conserva esta limitación hasta contar con
un runner y evidencia de compatibilidad.
