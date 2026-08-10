"""Credential-safe diagnostic report export."""

from __future__ import annotations

import json
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SENSITIVE_KEYS = {
    "auth_ref",
    "auth_password",
    "password",
    "token",
    "api_key",
    "secret",
    "cookie",
    "authorization",
    "proxy_password",
    "username_ref",
}

SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)=([^&\s\"']+)"),
    re.compile(r"(?i)(Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+"),
]


class DiagnosticReport:
    """Collects system/app state and exports it with secrets scrubbed."""

    def __init__(self, context) -> None:
        self.context = context

    # ----- collection -----

    def collect(self) -> dict:
        return {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "application": {
                "name": "MagnetoClip",
                "version": "0.1.0",
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
            "paths": {
                "config_dir": str(self.context.config_dir),
                "data_dir": str(self.context.data_dir),
                "log_dir": str(self.context.log_dir),
            },
            "settings": self._scrub_settings(self.context.settings.as_dict()),
            "downloads": self._download_summary(),
            "queues": self._queues_summary(),
            "categories": self._categories_summary(),
            "schedules": self._schedules_summary(),
        }

    def _download_summary(self) -> list[dict]:
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return []
        snapshots = manager.list_snapshots(limit=200)
        return [
            {
                "id": s["id"],
                "filename": s["filename"],
                "status": s["status"],
                "size_total": s["size_total"],
                "size_downloaded": s["size_downloaded"],
                "speed_avg": s["speed"],
                "connections": f"{s['connections_active']}/{s['connections_max']}",
                "detected_type": s["detected_type"],
                "error": s["error"],
            }
            for s in snapshots
        ]

    def _queues_summary(self) -> list[dict]:
        queues = getattr(self.context, "queues", None)
        if queues is None:
            return []
        return [
            {"id": q.id, "name": q.name, "max_concurrent": q.max_concurrent}
            for q in queues.list()
        ]

    def _categories_summary(self) -> list[dict]:
        categories = getattr(self.context, "categories", None)
        if categories is None:
            return []
        return [
            {"id": c.id, "name": c.name, "folder": c.folder} for c in categories.list()
        ]

    def _schedules_summary(self) -> list[dict]:
        scheduler = getattr(self.context, "scheduler", None)
        if scheduler is None:
            return []
        return [
            {
                "id": s.id,
                "name": s.name,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "days_mask": s.days_mask,
                "enabled": s.enabled,
            }
            for s in scheduler.schedules()
        ]

    # ----- scrubbing -----

    @classmethod
    def _scrub_settings(cls, settings: dict) -> dict:
        def clean(key: str, value):
            if key.lower() in SENSITIVE_KEYS or any(
                part in key.lower() for part in SENSITIVE_KEYS
            ):
                return "***REDACTED***"
            if isinstance(value, str):
                value = cls._scrub_text(value)
            return value

        return {key: clean(key, value) for key, value in settings.items()}

    @staticmethod
    def _scrub_text(text: str) -> str:
        for pattern in SENSITIVE_PATTERNS:
            text = pattern.sub(lambda m: f"{m.group(1)}=***REDACTED***", text)
        return text

    # ----- export -----

    def export(self, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = self.collect()
        destination.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        return destination

    def assert_no_secrets(self, report: dict | None = None) -> None:
        """Raise AssertionError if any collected value still holds secrets."""
        text = json.dumps(report or self.collect(), default=str)
        leaked = [pattern.pattern for pattern in SENSITIVE_PATTERNS if pattern.search(text)]
        assert not leaked, f"Report leaks secrets matching: {leaked}"
