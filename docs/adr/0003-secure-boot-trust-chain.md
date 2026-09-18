# ADR 0003: cadena de confianza y gestión de claves para Secure Boot

**Estado:** aceptado el 18 de septiembre de 2026.

**Issue:** [#63](https://github.com/cavazquez/pyfog/issues/63).

## Contexto

El MVP usa UEFI sin Secure Boot. Habilitarlo exige distinguir la confianza del firmware de la
autenticación del coordinador: una imagen o un agente no deben poder introducir una clave raíz, y
PyFog no debe convertirse en custodio de la clave privada que autoriza el arranque de una flota.
El smoke OVMF con Secure Boot y la firma de artefactos quedan para el issue [#64](https://github.com/cavazquez/pyfog/issues/64).

## Decisión

La raíz de confianza pertenece al operador de la plataforma, no al repositorio ni a PyFog:

1. El operador conserva offline la **PK** (Platform Key), la **KEK** (Key Exchange Key), la base
   **db** de certificados permitidos y la base **dbx** de revocaciones. Sólo el firmware del equipo
   acepta cambios de PK/KEK/db/dbx.
2. El operador incorpora a `db` el certificado público de firma de releases de PyFog. La privada
   correspondiente permanece en una HSM o un servicio de firma offline con control de dos personas.
   La PK/KEK del cliente nunca se entrega a PyFog.
3. La primera etapa EFI `BOOTX64.EFI`/shim se firma con ese certificado y el firmware valida su
   firma PE/COFF contra `db`, respetando `dbx` y la política SBAT del shim.
4. shim valida `grubx64.efi` con su certificado de proveedor; GRUB valida el kernel y la
   configuración de arranque antes de cargarla. El kernel valida los módulos firmados según la
   política de la distribución. El initramfs y el manifiesto del agente llevan además hashes en el
   paquete release; el agente verifica esos hashes antes de ejecutar una operación.
5. Si se distribuyen `mmx64.efi`, `fbx64.efi` u otros binarios EFI de la misma cadena, se firman y
   se verifican con el mismo procedimiento. OVMF, el firmware del equipo, GRUB upstream y los
   binarios de terceros conservan sus propietarios y claves; PyFog no los re-firma sin autorización.

La firma de artefactos EFI es una etapa de publicación separada de la captura. Un resultado de
captura nunca puede modificar db/dbx ni convertirse automáticamente en un binario confiable.
Un pipeline sin acceso al firmador falla cerrado: no existe fallback a una publicación sin firma.

## Propietarios y ciclo de vida

| Material | Propietario | Almacenamiento y uso |
| --- | --- | --- |
| PK | Operador de la plataforma | Offline/HSM; recuperación física del firmware. |
| KEK y db/dbx | Operador de la plataforma | Custodia separada; autoriza altas y revocaciones. |
| Clave privada de release | Equipo de release/security de PyFog | HSM o firmador offline; nunca Git, imagen, CI, argumento o log. |
| Certificados públicos | Operador y release | Se pueden publicar junto al manifiesto/procedencia para verificación. |
| CA HTTPS | Administrador de la instalación | Separada de Secure Boot; se rota según la política TLS. |

La rotación normal agrega el certificado nuevo a `db`, publica una release firmada con él, espera
la actualización de los equipos y recién después revoca el certificado anterior en `dbx`. Durante
la ventana de transición se aceptan ambas firmas sólo donde la política del operador lo permita.
Una clave comprometida detiene la publicación, revoca su certificado en `dbx`, rota el firmador,
marca las releases afectadas y redistribuye una cadena de recuperación aprobada por el operador.
La recuperación requiere acceso físico o un canal de administración del firmware; no se resuelve
embebiendo una clave privada en la imagen.

## Desarrollo, CI y release

- Desarrollo usa claves efímeras generadas en un directorio temporal ignorado y fixtures públicos.
  Nunca se reutilizan para una flota ni se suben sus privadas.
- CI verifica certificados y firmas públicas, ejecuta una firma inválida y una firma con una clave
  no confiable, y conserva sólo diagnóstico sin claves. El firmador de release recibe una ruta
  externa a la privada; ningún material secreto se copia a la publicación ni se escribe en logs.
  Un job de firma futuro podrá recibir una identidad OIDC y firmar dentro del HSM.
- Release exige un manifiesto de procedencia, checksums, revisión de dos personas y firma de
  `BOOTX64.EFI`, shim, GRUB y los demás EFI que se publiquen. `pxe/build-pxe --secure-boot`
  genera el manifiesto de firmas y el bundle falla cerrado si falta una firma o una revocación
  vigente.
- Logs, argumentos, DHCP, iPXE, manifiestos de imagen y artefactos de staging no contienen
  privadas, PINes, tokens del firmador ni material de recuperación. Los certificados públicos y
  fingerprints sí pueden registrarse.

## Verificación de esta ADR

`scripts/secure_boot_policy.py` mantiene la prueba criptográfica aislada con claves temporales.
`scripts/secure_boot_artifacts.py` agrega firma Authenticode reproducible de EFI, verificación del
manifiesto, alteración, artefacto sin firma y clave no confiable. `lab/e2e.sh run --secure-boot`
comprueba el arranque de source y target con OVMF secboot y NVRAM de prueba.

## Consecuencias

Secure Boot no se habilita automáticamente para cualquier imagen capturada: la compatibilidad de
imágenes sigue requiriendo una política explícita de distribución, SBAT, db y dbx. El camino de
publicación EFI, el smoke OVMF y el informe E2E quedan implementados por #64 sin convertir a PyFog
en custodio de las claves del operador.
