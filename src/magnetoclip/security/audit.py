"""Runtime security audit for settings and data exposure."""

from __future__ import annotations

from pathlib import Path

HIGH_RISK_SETTINGS = (
    "network.proxy_password",
    "auth.password",
    "auth.token",
    "browser.cookie",
)


class SecurityAudit:
    """Checks the current configuration for common security risks."""

    def __init__(self, context) -> None:
        self.context = context

    def findings(self) -> list[dict]:
        """Return a list of ``{level, message}`` findings (empty = clean)."""
        result: list[dict] = []

        settings = self.context.settings.as_dict()
        for key in HIGH_RISK_SETTINGS:
            value = settings.get(key)
            if value:
                result.append(
                    {
                        "level": "warning",
                        "message": f"Sensitive setting '{key}' is stored in plaintext",
                    }
                )

        max_connections = int(self.context.settings.get("downloads.connections_per_download", 8) or 0)
        if max_connections > 32:
            result.append(
                {
                    "level": "warning",
                    "message": "Per-download connection count is very high; may overload servers",
                }
            )

        return result

    @staticmethod
    def check_directory_traversal(directory: Path, filename: str) -> bool:
        """Return True if ``filename`` is safely contained under ``directory``."""
        from magnetoclip.security.safe_names import UnsafePathError, safe_join

        try:
            safe_join(directory, filename)
        except (UnsafePathError, OSError):
            return False
        return True
