"""Built-in file preview: resolve and render previewable files."""

from __future__ import annotations

from .resolver import PreviewResolver, PreviewType

__all__ = ["PreviewResolver", "PreviewService", "PreviewType"]


class PreviewService:
    """Facade that answers whether and how a file can be previewed."""

    def __init__(self, context) -> None:
        self.context = context
        self.resolver = PreviewResolver(context)

    def resolve(self, path) -> PreviewType:
        return self.resolver.resolve(path)

    def type_of_download(self, download) -> PreviewType:
        return self.resolver.type_of_download(download)

    def filenames(self, download) -> list[str]:
        return self.resolver.filenames(download)

    def close(self) -> None:
        pass
