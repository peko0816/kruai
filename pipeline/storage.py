"""Where a pack's audio goes when it is imported (BACKLOG E7).

PRD 6.3: the runtime only ever reads a URL. Something has to put the files
where that URL points, and that something is the import — the last stage, and
the only one that talks to infrastructure.

Only the local store is implemented. ``OBJECT_STORAGE_ENDPOINT`` is empty in
every configuration that exists today and must stay optional (L-5, D-002),
reaching for a vendor SDK would need a provider abstraction that M0 has not
scoped, and an importer that quietly wrote files to a laptop while claiming to
have uploaded them is the worst of the three. So a configured endpoint is
refused by name rather than half-served (D-097).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.core.config import Settings


class StorageNotImplementedError(RuntimeError):
    """A storage backend that has been configured but not written."""


class MediaStore(Protocol):
    """Somewhere to put a file and get back the URL it will be served from."""

    def put(self, path: str, data: bytes) -> str: ...


class LocalMediaStore:
    """Writes under a directory and hands back ``base_url`` + path.

    The directory is what a CDN or a static server is pointed at. With no
    ``PUBLIC_MEDIA_BASE_URL`` the URL is a relative path, which is enough for a
    test and honest about being unserved.
    """

    def __init__(self, root: Path, *, base_url: str = "") -> None:
        self.root = root
        self.base_url = base_url.rstrip("/")

    def put(self, path: str, data: bytes) -> str:
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return f"{self.base_url}/{path}" if self.base_url else path


def get_media_store(settings: Settings, *, root: Path) -> MediaStore:
    """The configured store.

    Raises:
        StorageNotImplementedError: OBJECT_STORAGE_ENDPOINT is set. Refusing is
            the point: importing against a configured bucket and writing to the
            local disk instead would leave a database full of URLs that answer
            404, discovered by a learner rather than by this function.
    """
    if settings.object_storage_endpoint.strip():
        raise StorageNotImplementedError(
            f"OBJECT_STORAGE_ENDPOINT={settings.object_storage_endpoint!r} is set, but no "
            "object-storage backend is implemented yet (docs/DECISIONS.md D-097). "
            "Clear it to publish locally, or implement the backend first — importing "
            "against a bucket that is never written would fill the database with URLs "
            "that answer 404."
        )
    return LocalMediaStore(root, base_url=settings.public_media_base_url)
