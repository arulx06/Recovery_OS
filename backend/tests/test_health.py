import socket

import pytest


def test_health_reports_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "recoveryos-backend"
    assert body["status"] == "ok"
    assert body["database"] == "connected"


def test_cases_returns_list(client):
    resp = client.get("/cases")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_external_network_is_denied_suite_wide():
    with pytest.raises(AssertionError, match="must not access external networks"):
        socket.create_connection(("example.invalid", 443))

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        with pytest.raises(AssertionError, match="must not access external networks"):
            udp.sendto(b"test", ("192.0.2.1", 53))
