#!/usr/bin/env python3
"""Validate a PyFog image manifest and, optionally, its partition artifacts."""

import argparse
import sys
from pathlib import Path

from pyfog.image_manifest import validate_image_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Ruta al manifest.json de la imagen")
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        help="Directorio raíz donde se encuentran los artefactos referenciados",
    )
    args = parser.parse_args()
    try:
        manifest = validate_image_manifest(args.manifest, args.artifacts_dir)
    except ValueError as error:
        parser.exit(1, f"Error: {error}\n")
    sys.stdout.write(
        f"Manifiesto {manifest.image_id} válido: "
        f"{len(manifest.disk.partitions)} particiones, {len(manifest.artifacts)} artefactos."
        "\n"
    )


if __name__ == "__main__":
    main()
