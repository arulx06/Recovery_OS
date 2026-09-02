def test_health_reports_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "recoveryos-backend"
    assert body["status"] in ("ok", "degraded")


def test_cases_returns_list(client):
    resp = client.get("/cases")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
