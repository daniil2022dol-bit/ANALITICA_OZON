import base64
import hashlib
import hmac
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from ozon_analytics import web
from ozon_analytics.auth import hash_password, verify_password


def test_password_hash_rejects_wrong_password_and_malformed_values():
    encoded = hash_password("test-only-password")
    assert verify_password("test-only-password", encoded)
    assert not verify_password("other-password", encoded)
    assert not verify_password("test-only-password", "broken")
    assert hash_password("test-only-password") != encoded


def test_partner_has_identical_dashboard_access_and_can_be_revoked(monkeypatch):
    encoded = hash_password("test-only-partner")
    monkeypatch.setattr(
        web,
        "settings",
        replace(web.settings, dashboard_users_json=json.dumps({"partner": encoded})),
    )
    monkeypatch.setattr(
        web,
        "dashboard",
        lambda *args: {"sales": [{"units": 2}], "stocks": [{"units": 7}]},
    )
    web.attempts.clear()
    results = []
    for username, password in [
        ("analytics", "test-password"),
        ("partner", "test-only-partner"),
    ]:
        with TestClient(web.app, base_url="https://testserver") as client:
            bad = client.post(
                "/login",
                data={"username": username, "password": "incorrect"},
                follow_redirects=False,
            )
            assert bad.headers["location"] == "/login?error=1"
            assert "set-cookie" not in bad.headers
            response = client.post(
                "/login",
                data={"username": username, "password": password},
                follow_redirects=False,
            )
            assert response.headers["location"] == "/"
            assert client.get("/").status_code == 200
            result = client.get(
                "/api/dashboard?date_from=2026-10-01&date_to=2026-10-07"
            )
            assert result.status_code == 200
            results.append(result.json())
            if username == "partner":
                token = client.cookies.get("ozon_session").strip('"')
                assert web.valid_session(token)
                monkeypatch.setattr(
                    web, "settings", replace(web.settings, dashboard_users_json="{}")
                )
                assert not web.valid_session(token)
                assert client.get("/api/dashboard").status_code == 401
    assert results[0] == results[1]


def test_primary_session_survives_upgrade_and_extra_cannot_override_primary(
    monkeypatch,
):
    monkeypatch.setattr(
        web,
        "settings",
        replace(web.settings, dashboard_users_json='{"analytics":"invalid"}'),
    )
    data = base64.urlsafe_b64encode(b"2000:legacy-random-nonce").decode()
    signature = hmac.new(
        web.settings.session_secret.encode(), data.encode(), hashlib.sha256
    ).hexdigest()
    assert web.valid_session(data + "." + signature, now=1000)
    assert not web.valid_session(data + "." + signature, now=2001)
    assert web.configured_users()["analytics"] == ("plain", "test-password")
