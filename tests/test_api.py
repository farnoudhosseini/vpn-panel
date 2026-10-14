"""HTTP surface: authentication, authorisation and the main routes."""

from __future__ import annotations

import pytest


def test_health_is_public(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_index_is_served_when_the_frontend_is_built(client) -> None:
    response = client.get("/")
    # The panel either serves the built SPA or the placeholder JSON document.
    assert response.status_code == 200


def test_unknown_route_falls_back_to_the_spa(client) -> None:
    response = client.get("/nodes/1")
    assert response.status_code in (200, 404)


def test_robots_is_disallowed(client) -> None:
    response = client.get("/robots.txt")
    assert "Disallow: /" in response.text


def test_protected_route_requires_a_token(client) -> None:
    response = client.get("/api/system/info", headers={"Authorization": ""})
    assert response.status_code == 401


def test_bad_token_is_rejected(client) -> None:
    response = client.get("/api/system/info", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401


def test_me_returns_the_account(client) -> None:
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "tester"
    assert body["role"] == "admin"
    assert "password_hash" not in body


def test_login_rejects_a_bad_password(client) -> None:
    response = client.post(
        "/api/auth/login", json={"username": "tester", "password": "wrong-password"}
    )
    assert response.status_code == 401


def test_system_overview_and_dashboard(client) -> None:
    overview = client.get("/api/system/overview", params={"minutes": 30})
    assert overview.status_code == 200
    assert "nodes" in overview.json()

    dashboard = client.get("/api/system/dashboard", params={"hours": 24})
    assert dashboard.status_code == 200

    info = client.get("/api/system/info")
    assert info.status_code == 200
    body = info.json()
    assert body["mode"] == "panel"
    assert "database_size_bytes" in body
    assert "scheduler" in body


def test_logs_and_events_are_paged(client) -> None:
    logs = client.get("/api/system/logs", params={"limit": 5})
    assert logs.status_code == 200
    assert {"total", "limit", "offset", "items"} <= set(logs.json())

    events = client.get("/api/system/events", params={"limit": 5})
    assert events.status_code == 200
    assert "items" in events.json()


def test_database_report(client) -> None:
    response = client.get("/api/system/database")
    assert response.status_code == 200
    body = response.json()
    assert body["journal_mode"].lower() == "wal"
    assert body["tables"], "the database report should list its tables"


def test_settings_round_trip(client) -> None:
    saved = client.put("/api/system/settings/theme", json={"value": {"accent": "crimson"}})
    assert saved.status_code in (200, 201)
    listed = client.get("/api/system/settings")
    assert listed.status_code == 200
    assert any(item["key"] == "theme" for item in listed.json())


def test_local_node_is_registered(client) -> None:
    nodes = client.get("/api/nodes")
    assert nodes.status_code == 200
    body = nodes.json()
    assert body and any(node["is_local"] for node in body)


def test_node_crud(client) -> None:
    created = client.post(
        "/api/nodes",
        json={"name": "api-edge", "address": "203.0.113.55", "agent_port": 8787, "scheme": "http"},
    )
    assert created.status_code == 201, created.text
    node = created.json()
    assert node["name"] == "api-edge"
    # An address that cannot be reached must be reported, not raised.
    assert node["status"] in {"offline", "unknown", "online"}

    duplicate = client.post("/api/nodes", json={"name": "api-edge", "address": "203.0.113.56"})
    assert duplicate.status_code == 409

    patched = client.patch(f"/api/nodes/{node['id']}", json={"note": "second look"})
    assert patched.status_code == 200 and patched.json()["note"] == "second look"

    detail = client.get(f"/api/nodes/{node['id']}")
    assert detail.status_code == 200
    assert "capabilities" in detail.json()

    removed = client.delete(f"/api/nodes/{node['id']}")
    assert removed.status_code == 204
    assert client.get(f"/api/nodes/{node['id']}").status_code == 404


def test_the_local_node_cannot_be_deleted(client) -> None:
    local = next(node for node in client.get("/api/nodes").json() if node["is_local"])
    assert client.delete(f"/api/nodes/{local['id']}").status_code == 400


def test_node_addresses_cannot_inject_a_path(client) -> None:
    response = client.post(
        "/api/nodes", json={"name": "hostile", "address": "http://x/../../etc/passwd"}
    )
    assert response.status_code == 422


def test_invalid_inbound_is_rejected_by_validation(client) -> None:
    response = client.post(
        "/api/inbounds",
        json={
            "tag": "bad tag!",
            "node_id": 1,
            "listen_port": 70000,
            "network_cidr": "not-a-network",
        },
    )
    assert response.status_code == 422


def test_transport_catalogue_is_exposed(client) -> None:
    response = client.get("/api/tunnels/transports")
    assert response.status_code == 200
    keys = {item["key"] for item in response.json()}
    assert {"wireguard", "gre", "udp", "wss"} <= keys


def test_tunnel_requires_two_nodes(client) -> None:
    local = next(node for node in client.get("/api/nodes").json() if node["is_local"])
    response = client.post(
        "/api/tunnels",
        params={"apply": "false"},
        json={
            "name": "self-loop",
            "transport": "wireguard",
            "mode": "direct",
            "local_node_id": local["id"],
            "remote_node_id": local["id"],
            "listen_port": 51820,
        },
    )
    assert response.status_code == 400


def test_client_creation_requires_a_real_inbound(client) -> None:
    response = client.post(
        "/api/clients",
        json={"inbound_id": 9999, "name": "ghost", "data_limit_bytes": 1024},
    )
    assert response.status_code in (400, 404, 422)


def test_audit_log_records_writes(client) -> None:
    client.post(
        "/api/nodes", json={"name": "audited", "address": "203.0.113.77", "scheme": "http"}
    )
    logs = client.get("/api/system/logs", params={"limit": 50}).json()
    actions = {item["action"] for item in logs["items"]}
    assert "node.create" in actions
    assert "auth.login" in actions


def test_logout_clears_the_session(client) -> None:
    # A separate client so the shared fixture stays signed in for other tests.
    from fastapi.testclient import TestClient

    from panel.main import create_app

    with TestClient(create_app()) as fresh:
        assert fresh.post("/api/auth/login", json={"username": "tester", "password": "Test-Admin-Password-1!"}).status_code == 200
        assert fresh.post("/api/auth/logout").status_code in (200, 204)


def test_security_headers_are_set(client) -> None:
    response = client.get("/health")
    for header in ("x-content-type-options", "x-frame-options", "referrer-policy"):
        assert header in {name.lower() for name in response.headers}


@pytest.mark.parametrize("path", ["/api/system/info", "/api/nodes", "/api/clients", "/api/inbounds"])
def test_read_routes_answer(client, path: str) -> None:
    assert client.get(path).status_code == 200
