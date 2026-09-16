"""Catalog rules shared by image web pages and future task validation."""

from pyfog.models import Image

IMAGE_STATUSES = ("draft", "capturing", "ready", "failed")


def image_is_selectable(image: Image) -> bool:
    """Only a ready image with an integrity timestamp and manifest can be deployed."""

    return (
        image.status == "ready"
        and image.integrity_verified_at is not None
        and image.manifest_json is not None
    )
