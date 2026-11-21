"""Dialog widgets."""

from __future__ import annotations

from .add_url import AddUrlDialog
from .capture import CaptureDialog
from .preview import PreviewDialog
from .proxy import ProxyDialog
from .stream_quality import StreamingQualityDialog

__all__ = [
    "AddUrlDialog",
    "CaptureDialog",
    "PreviewDialog",
    "ProxyDialog",
    "StreamingQualityDialog",
]