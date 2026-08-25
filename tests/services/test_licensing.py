"""Client licensing round-trips against the real Flask license server."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx
import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "license-server"
sys.path.insert(0, str(SERVER_DIR))

from mclip_license import create_app, new_serial  # noqa: E402
from mclip_license.db import License  # noqa: E402
from mclip_license.signing import (  # noqa: E402
    generate_private_key_pem,
    public_key_b64_from_private_pem,
)

from magnetoclip.services.licensing.client import (  # noqa: E402
    BadSignature,
    BoundToOtherPC,
    canonical_serial_input,
    LicenseClient,
    MachineLimitReached,
    NetworkUnavailable,
    Revoked,
    UnknownSerial,
)
from magnetoclip.services.licensing.fingerprint import machine_id  # noqa: E402
from magnetoclip.services.licensing.state import (  # noqa: E402
    format_masked_serial,
)

M1 = "a" * 64
M2 = "b" * 64


@pytest.fixture()
def server_app(tmp_path):
    pem = generate_private_key_pem()
    app = create_app(
        db_path=tmp_path / "licenses.db",
        admin_password="pw",
        signing_key_pem=pem,
        secret_key=b"unit-tests" * 4,
    )
    app.config["TESTING"] = True
    return app, public_key_b64_from_private_pem(pem)


@pytest.fixture()
def client(server_app):
    app, public_key = server_app
    transport = httpx.WSGITransport(app=app)
    return LicenseClient(
        "http://testserver", public_key_b64=public_key, transport=transport
    )


def _make_license(app, **kwargs) -> str:
    factory = app.extensions["db_factory"]
    with factory() as session:
        lic = License(serial=new_serial(), **kwargs)
        session.add(lic)
        session.commit()
        return lic.serial


class TestRoundTrips:
    def test_activate_and_validate_ok(self, server_app, client):
        app, pub = server_app
        serial = _make_license(app)
        data = client.activate(serial, M1, hostname="TEST-PC")
        assert data["status"] == "ok"
        assert client.last_signature_ok is True

        vdata = client.validate(serial, M1)
        assert vdata["action"] == "validate"

    def test_second_machine_hits_limit_with_hostname(self, server_app, client):
        app, _ = server_app
        serial = _make_license(app, owner_label="cust@example.com")
        client.activate(serial, M1, hostname="OFFICE-PC")
        with pytest.raises(MachineLimitReached) as excinfo:
            client.activate(serial, M2, hostname="LAPTOP")
        assert excinfo.value.extra["bound_hostname"] == "OFFICE-PC"

    def test_wrong_machine_validate_reports_binding(self, server_app, client):
        app, _ = server_app
        serial = _make_license(app)
        client.activate(serial, M1, hostname="HOME-PC")
        with pytest.raises(BoundToOtherPC) as excinfo:
            client.validate(serial, M2)
        assert excinfo.value.extra["bound_hostname"] == "HOME-PC"

    def test_revocation_detected_on_next_launch(self, server_app, client):
        app, _ = server_app
        serial = _make_license(app)
        client.activate(serial, M1)

        factory = app.extensions["db_factory"]
        with factory() as session:
            lic = session.query(License).filter_by(serial=serial).one()
            lic.status = "revoked"
            session.commit()

        with pytest.raises(Revoked):
            client.validate(serial, M1)

    def test_deactivate_then_rebind(self, server_app, client):
        app, _ = server_app
        serial = _make_license(app)
        client.activate(serial, M1)
        client.deactivate(serial, M1)
        assert client.activate(serial, M2).get("status") == "ok"

    def test_unknown_serial(self, client):
        with pytest.raises(UnknownSerial):
            client.activate("MGCL-AAAAA-BBBBB-CCCCC-DDDDD", M1)


class TestSecurity:
    def test_tampered_response_rejected(self, server_app):
        """A MITM rewriting the payload must fail signature verification."""
        app, pub = server_app
        serial = _make_license(app)

        class TamperingTransport(httpx.BaseTransport):
            def handle_request(self, request):
                real = httpx.WSGITransport(app=app)
                response = real.handle_request(request)
                body = httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=response.read(),
                    request=request,
                )
                payload = body.json()
                payload["data"]["status"] = "forged"
                import json

                forged = json.dumps(payload).encode()
                return httpx.Response(
                    response.status_code,
                    content=forged,
                    headers={"content-type": "application/json"},
                    request=request,
                )

        evil = LicenseClient(
            "http://testserver", public_key_b64=pub, transport=TamperingTransport()
        )
        with pytest.raises(BadSignature):
            evil.activate(serial, M1)

    def test_unreachable_endpoint_maps_to_network_error(self):
        lonely = LicenseClient("http://127.0.0.1:9", timeout=0.5)
        with pytest.raises(NetworkUnavailable):
            lonely.activate("MGCL-AAAAA-BBBBB-CCCCC-DDDDD", M1)


class TestHelpers:
    def test_machine_id_is_stable_hex64(self):
        first = machine_id()
        assert re.fullmatch(r"[0-9a-f]{64}", first)
        assert machine_id() == first

    def test_serial_input_canonicalized(self):
        assert (
            canonical_serial_input("mgcl abcde-fghjk lmnop rstuv")
            == "MGCL-ABCDE-FGHJK-LMNOP-RSTUV"
        )

    def test_masked_serial(self):
        assert format_masked_serial("MGCL-ABCDE-FGHJK-LMNOP-QRSTU") == (
            "MGCL-*****-*****-*****-QRSTU"
        )
        assert format_masked_serial("") == "—"
