"""HTTP client for the license API with Ed25519 response verification."""

from __future__ import annotations

import re

import httpx

from .signing import verify_payload

DEFAULT_TIMEOUT = 8.0


class LicenseError(Exception):
    """Base class; ``code`` matches server error identifiers."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code or self.__class__.__name__.lower()
        self.extra: dict = {}


class UnknownSerial(LicenseError):
    def __init__(self) -> None:
        super().__init__("This serial key was not recognized.", "unknown_serial")


class Revoked(LicenseError):
    def __init__(self) -> None:
        super().__init__(
            "This serial key has been revoked by the vendor.", "revoked"
        )


class Expired(LicenseError):
    def __init__(self) -> None:
        super().__init__("This serial key has expired.", "expired")


class MachineLimitReached(LicenseError):
    def __init__(self, bound_hostname: str = "", max_machines: int = 1) -> None:
        where = f" ({bound_hostname})" if bound_hostname else ""
        super().__init__(
            f"This serial is already in use on another PC{where}. "
            "Deactivate it there first or contact support.",
            "limit_reached",
        )
        self.extra = {"bound_hostname": bound_hostname, "max_machines": max_machines}


class BoundToOtherPC(MachineLimitReached):
    def __init__(self, bound_hostname: str = "") -> None:
        super().__init__(bound_hostname, 1)
        self.code = "bound_to_other_pc"


class NotBound(LicenseError):
    def __init__(self) -> None:
        super().__init__("This PC is not activated with this serial.", "not_bound")


class RateLimited(LicenseError):
    def __init__(self) -> None:
        super().__init__("Too many attempts — try again in a minute.", "rate_limited")


class ServerError(LicenseError):
    pass


class NetworkUnavailable(LicenseError):
    def __init__(self, detail: str = "") -> None:
        msg = "Could not reach the license server."
        if detail:
            msg += f" ({detail})"
        super().__init__(msg, "network")
        self.detail = detail


class BadSignature(LicenseError):
    def __init__(self) -> None:
        super().__init__(
            "License server responded with an invalid signature.", "bad_signature"
        )


_ERROR_MAP: dict[str, type[LicenseError]] = {
    "unknown_serial": UnknownSerial,
    "revoked": Revoked,
    "expired": Expired,
    "rate_limited": RateLimited,
}


def canonical_serial_input(raw: str) -> str:
    """User-tolerant cleanup: uppercase, keep MGCL prefix, re-group by five."""
    cleaned = re.sub(r"[^0-9A-Za-z]", "", raw or "").upper()
    prefix = ""
    if cleaned.startswith("MGCL"):
        prefix, cleaned = "MGCL", cleaned[4:]
    grouped = "-".join(cleaned[i : i + 5] for i in range(0, len(cleaned), 5))
    return f"{prefix}-{grouped}" if prefix else grouped


class LicenseClient:
    def __init__(
        self,
        endpoint: str,
        public_key_b64: str | None = None,
        app_version: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        # Empty key = unpinned dev mode; production releases embed the real key.
        self.public_key_b64 = public_key_b64 or ""
        self.app_version = app_version
        self.timeout = timeout
        self._transport = transport
        self.last_signature_ok: bool | None = None

    def _post(self, path: str, payload: dict) -> dict:
        url = self.endpoint + path
        try:
            if self._transport is not None:
                with httpx.Client(
                    transport=self._transport,
                    timeout=self.timeout,
                    follow_redirects=False,
                ) as client:
                    resp = client.post(url, json=payload)
            else:
                resp = httpx.post(
                    url, json=payload, timeout=self.timeout, follow_redirects=False
                )
        except httpx.HTTPError as exc:
            raise NetworkUnavailable(str(exc.__cause__ or exc)) from exc

        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass

        if resp.is_success and isinstance(body.get("data"), dict):
            sig = body.get("sig") or ""
            if self.public_key_b64:
                self.last_signature_ok = verify_payload(
                    self.public_key_b64, body["data"], sig
                )
                if not self.last_signature_ok:
                    raise BadSignature()
            else:
                self.last_signature_ok = None
            return body["data"]

        code = str(body.get("error", ""))
        if not code:
            raise ServerError(f"HTTP {resp.status_code}", f"http_{resp.status_code}")
        cls = _ERROR_MAP.get(code)
        if cls is not None:
            raise cls()
        if code == "limit_reached":
            raise MachineLimitReached(
                str(body.get("bound_hostname", "")),
                int(body.get("max_machines", 1)),
            )
        if code == "bound_to_other_pc":
            raise BoundToOtherPC(str(body.get("bound_hostname", "")))
        if code == "not_bound":
            raise NotBound()
        raise ServerError(code, code)

    def activate(
        self,
        serial: str,
        fingerprint: str,
        hostname: str = "",
    ) -> dict:
        return self._post(
            "/v1/activate",
            {
                "serial": serial,
                "machine_id": fingerprint,
                "hostname": hostname[:128],
                "app_version": self.app_version[:32],
            },
        )

    def validate(self, serial: str, fingerprint: str) -> dict:
        return self._post(
            "/v1/validate", {"serial": serial, "machine_id": fingerprint}
        )

    def deactivate(self, serial: str, fingerprint: str) -> dict:
        return self._post(
            "/v1/deactivate", {"serial": serial, "machine_id": fingerprint}
        )
