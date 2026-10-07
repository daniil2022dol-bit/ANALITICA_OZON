from fastapi.testclient import TestClient

from ozon_analytics import web


def test_sessions_expire_and_reject_tampering():
    token = web.create_session(now=1000)
    assert web.valid_session(token, now=1001)
    assert not web.valid_session(token, now=1000 + 7 * 86400)
    for value in [None, "", "bad", "a.b", "%%%." + "a" * 64, token + "x"]:
        assert not web.valid_session(value, now=1001)


def test_authentication_and_phone_dashboard_routes(db):
    web.attempts.clear()
    with TestClient(web.app, base_url="https://testserver") as client:
        assert client.get("/api/dashboard").status_code == 401
        assert client.get("/login").status_code == 200
        assert client.get("/healthz").json() == {"status": "ok"}
        bad_origin = client.post(
            "/login",
            headers={"origin": "https://evil.example"},
            data={"username": "analytics", "password": "test-password"},
        )
        assert bad_origin.status_code == 403
        response = client.post(
            "/login",
            data={"username": "analytics", "password": "test-password"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/").status_code == 200
        data = client.get("/api/dashboard?date_from=2026-10-01&date_to=2026-10-07")
        assert data.status_code == 200
        assert data.json()["snapshot_date"] is None
        assert (
            client.get(
                "/api/dashboard?date_from=2026-10-07&date_to=2026-10-01"
            ).status_code
            == 422
        )
        client.post("/logout")
        assert client.get("/api/dashboard").status_code == 401
