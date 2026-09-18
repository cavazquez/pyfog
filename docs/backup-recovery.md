# Backup y recuperación

El backup del MVP combina una copia consistente de SQLite, el almacén completo de imágenes
(`published` y `staging`) y un manifiesto con SHA-256. El comando no incluye contraseñas, sesiones,
tokens de equipos, claves de sesión, certificados TLS ni claves privadas.

En una instalación con RBAC, crear un backup requiere `backup.create`, verificarlo requiere
`backup.read` y recuperar una instalación requiere `backup.restore`. La interfaz de roles está
documentada en [Roles y permisos](rbac.md); estos comandos de mantenimiento se ejecutan fuera de
la web y además quedan protegidos por la cuenta del sistema operativo que administra el servicio.

## Crear y verificar

Detené tareas nuevas o anotá las que estén en ejecución. El backup usa la operación de copia de
SQLite, por lo que no hace falta detener el proceso para obtener un snapshot consistente; mantener
el servicio en modo mantenimiento evita que se acumulen temporales mientras se copia el almacén.

```bash
uv run python -m scripts.backup_server backup \
  --database-url sqlite:////var/lib/pyfog/pyfog.db \
  --image-store /var/lib/pyfog/pyfog-images \
  --output /var/backups/pyfog-$(date -u +%Y%m%dT%H%M%SZ)

uv run python -m scripts.backup_server verify \
  --backup /var/backups/pyfog-YYYYMMDDTHHMMSSZ
```

La verificación comprueba las sumas de todos los archivos, referencias foráneas, manifiestos,
publicaciones, tamaños y hashes de cada artefacto. Un backup que no pasa esta etapa no debe
restaurarse.

## Recuperar una instalación vacía

Prepará un directorio de aplicación nuevo, instalá la misma versión de PyFog y apuntá la orden al
archivo SQLite y almacén vacíos. La orden verifica el backup antes de escribir y se niega a usar
destinos con datos existentes:

```bash
uv run python -m scripts.backup_server restore \
  --backup /var/backups/pyfog-YYYYMMDDTHHMMSSZ \
  --database-url sqlite:////var/lib/pyfog-new/pyfog.db \
  --image-store /var/lib/pyfog-new/pyfog-images
PYFOG_DATABASE_URL=sqlite:////var/lib/pyfog-new/pyfog.db uv run alembic upgrade head
```

La recuperación borra todas las sesiones web copiadas y marca como
`intervention_required` las tareas que tenían un intento sin terminar. Conserva su reserva,
intento e historial para que un operador confirme que el agente anterior está detenido antes de
reconciliar y crear un intento nuevo. Las tareas aprobadas sin intento pueden revisarse y seguir en
cola. Emití nuevos tokens de agentes y reiniciá credenciales si el incidente pudo exponerlas.

## Claves, certificados y configuración

Guardá `PYFOG_SECRET_KEY` o el archivo indicado por `PYFOG_SECRET_KEY_FILE` en un gestor de
secretos independiente. Guardá también el certificado de servidor, la clave privada, la CA que
reciben los agentes y las variables de puertos/hosts/proxy del despliegue. No los agregues al repo
ni al directorio del backup. Después de recuperar:

1. Restaurá la clave de sesión y los certificados desde el gestor de secretos.
2. Verificá `PYFOG_ALLOWED_HOSTS`, `PYFOG_TRUSTED_PROXY_IPS`, la URL de base y el almacén.
3. Rotá tokens de inventario y capacidades de PXE; las sesiones web viejas ya fueron invalidadas.
4. Ejecutá `verify` nuevamente y luego habilitá el servicio y las tareas.

El script incluido soporta SQLite, que es la base del despliegue LAN del MVP. Para una base externa
usá su herramienta nativa de snapshot consistente y copiá el almacén con la misma política; antes
de habilitar PyFog, aplicá la verificación de referencias y artefactos en una instalación vacía.
