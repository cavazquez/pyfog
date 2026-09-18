# Procedencia y licencias de PyFog v0.1.0

Este inventario registra las dependencias y binarios que forman la entrega. Las versiones de Python
y de las dependencias de aplicación se fijan en `pyproject.toml` y `uv.lock`; antes de publicar una
release, `make release-check` y `make audit` deben pasar. Las licencias indicadas son las de los
proyectos upstream y deben conservarse junto con sus avisos cuando se redistribuyan binarios.

## Código y dependencias Python

| Componente | Versión fijada | Procedencia | Licencia upstream |
| --- | --- | --- | --- |
| PyFog | 0.1.0 | Este repositorio y `dist/release/manifest.json` | Apache-2.0 |
| Python | 3.12.13 en la imagen; 3.12/3.14 en CI | [python.org](https://www.python.org/) | PSF-2.0 |
| FastAPI | 0.141.1 | [github.com/fastapi/fastapi](https://github.com/fastapi/fastapi) | MIT |
| Starlette | 1.6.0 | [github.com/encode/starlette](https://github.com/encode/starlette) | BSD-3-Clause |
| Pydantic | 2.13.5 | [github.com/pydantic/pydantic](https://github.com/pydantic/pydantic) | MIT |
| Uvicorn | 0.53.0 | [github.com/encode/uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause |
| SQLAlchemy | 2.0.54 | [github.com/sqlalchemy/sqlalchemy](https://github.com/sqlalchemy/sqlalchemy) | MIT |
| Alembic | 1.20.0 | [github.com/sqlalchemy/alembic](https://github.com/sqlalchemy/alembic) | MIT |
| Jinja2 | 3.1.6 | [github.com/pallets/jinja](https://github.com/pallets/jinja) | BSD-3-Clause |
| python-multipart | 0.0.32 | [github.com/Kludex/python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 |
| itsdangerous | 2.2.0 | [github.com/pallets/itsdangerous](https://github.com/pallets/itsdangerous) | BSD-3-Clause |
| pwdlib | 0.3.1 | [github.com/frankie567/pwdlib](https://github.com/frankie567/pwdlib) | MIT |
| argon2-cffi | lockfile | [github.com/hynek/argon2-cffi](https://github.com/hynek/argon2-cffi) | MIT |

Las dependencias transitivas adicionales y sus hashes son las que aparecen en `uv.lock`. `pip-audit`
comprueba vulnerabilidades conocidas; el lockfile y la licencia del paquete concreto deben revisarse
si se actualiza una dependencia.

## Imagen y herramientas del agente

| Componente | Uso | Procedencia/licencia |
| --- | --- | --- |
| `python:3.12.13-slim-bookworm` | Imagen del servidor | [Docker Official Images](https://hub.docker.com/_/python), licencia Python/OS según sus avisos |
| `uv` 0.11.21 | Instalación reproducible | [astral-sh/uv](https://github.com/astral-sh/uv), MIT/Apache-2.0 |
| Caddy 2.11.4-alpine | TLS y proxy LAN | [caddyserver.com](https://github.com/caddyserver/caddy), Apache-2.0 |
| Ubuntu Server 24.04 LTS | Referencia del laboratorio | [Canonical cloud images](https://cloud-images.ubuntu.com/), Ubuntu licenses; URL y SHA-256 fijados en `lab/reference-image.env` |
| Partclone 0.3.45 | Copia de ESP/ext4 | [partclone.org](https://partclone.org/), GPL-2.0-or-later |
| `sgdisk`/`sfdisk`/`gdisk`, `zstd`, BusyBox, GRUB, `iproute2` | Layout GPT/MBR, compresión, red y arranque UEFI/BIOS | Proyectos/distribución Ubuntu; conservar avisos de sus paquetes |
| QEMU/OVMF | Laboratorio descartable | [qemu.org](https://www.qemu.org/), GPL-2.0-or-later / [edk2](https://github.com/tianocore/tianocore.github.io/wiki/Contributions), BSD-2-Clause |
| iPXE | Bootstrap PXE opcional | [ipxe.org](https://ipxe.org/), GPL-2.0-or-later con excepciones de imagen |

El repositorio no redistribuye binarios de iPXE, Ubuntu, QEMU, OVMF ni Partclone. El operador debe
obtenerlos de sus fuentes, verificar sus hashes/procedencia y cumplir sus términos. `manifest.json`
y `SHA256SUMS` registran los bytes efectivos del agente y del perfil PXE que sí se publican.
