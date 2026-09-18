# Política operativa de Secure Boot

La decisión normativa está en la [ADR 0003](adr/0003-secure-boot-trust-chain.md). Este documento
resume el checklist que debe aprobarse antes de habilitar el issue #64:

- [ ] El operador generó PK, KEK, db y dbx fuera del repositorio y tiene recuperación física.
- [ ] El certificado público de release fue incorporado a db por el operador.
- [ ] La clave privada de release está en HSM/firmador offline con doble control.
- [ ] `BOOTX64.EFI`/shim, GRUB, kernel y cada EFI publicado tienen firma verificable y SBAT/dbx
      revisados.
- [ ] El paquete release incluye procedencia, fingerprints y hashes, nunca privadas o PINes.
- [ ] CI pasó firma válida, firma alterada y firma con una clave no confiable.
- [ ] El smoke OVMF usa una NVRAM de prueba con Secure Boot habilitado y no toca claves de una
      máquina real.
- [ ] La rotación agrega la clave nueva antes de revocar la anterior; el plan de compromiso está
      probado y documentado.

La prueba automatizada disponible hoy es sólo la criptográfica de política:

```bash
make secure-boot-policy-check
```

No habilita Secure Boot en imágenes ni en el laboratorio actual. Esa integración queda bloqueada
hasta completar #64.
