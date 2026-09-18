# Política operativa de Secure Boot

La decisión normativa está en la [ADR 0003](adr/0003-secure-boot-trust-chain.md). Este documento
resume el checklist operativo que debe aprobarse antes de habilitar Secure Boot en una flota:

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

La integración de publicación y laboratorio está disponible con herramientas externas al
checkout. El firmador recibe la privada por una ruta controlada fuera del repositorio; la salida
contiene sólo el certificado público, fingerprints y hashes:

```bash
sudo apt-get install sbsigntool osslsigncode
pxe/build-pxe build \
  --agent-dir dist/agent \
  --base-url https://pyfog.example/boot \
  --ipxe-efi /ruta/controlada/ipxe.efi \
  --output-dir dist/pxe \
  --secure-boot \
  --signing-key /ruta/externa/release.key.pem \
  --signing-cert /ruta/externa/release.cert.pem
pxe/build-pxe verify --output-dir dist/pxe \
  --secure-boot-cert /ruta/externa/release.cert.pem
```

`--source-date-epoch` fija el tiempo de firma PE/COFF y permite repetir el resultado con la misma
entrada, certificado y clave. `secure-boot-manifest.json` no contiene privadas, PINes ni tokens.
La publicación falla cerrada si falta la firma, si se altera un artefacto o si se verifica con otro
certificado. Para ejecutar los negativos en CI o localmente:

```bash
make secure-boot-policy-check
make secure-boot-artifact-check
```

El laboratorio usa las imágenes `OVMF_CODE_4M.secboot.fd` y `OVMF_VARS_4M.ms.fd` cuando están
disponibles. La NVRAM se copia a `.lab/<vm>/OVMF_VARS.fd` y nunca se modifica la plantilla del
sistema:

```bash
./lab/pyfog-lab --secure-boot doctor
./lab/pyfog-lab --secure-boot up
./lab/e2e.sh run --secure-boot
```

El informe E2E guarda `firmware.log`, `secure_boot=enabled` y el estado de firma; un arranque
exitoso con Secure Boot habilitado se informa como `signature_status=verified-by-ovmf`. Los logs
no incluyen material privado ni credenciales. Para cambiar entre modos hay que destruir el
laboratorio descartable y recrearlo.
