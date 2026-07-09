from starlette.testclient import TestClient

from gigwa_mcp.__main__ import configure_http_transport_security
from gigwa_mcp.server import mcp


def _reset_streamable_http_session_manager() -> None:
    setattr(mcp, "_session_manager", None)


def test_streamable_http_app_allows_docker_service_host(monkeypatch) -> None:
    monkeypatch.setenv("GIGWA_MCP_ALLOWED_HOSTS", "gigwa-mcp")
    configure_http_transport_security()
    _reset_streamable_http_session_manager()

    app = mcp.streamable_http_app()
    with TestClient(app) as client:
        response = client.get("/mcp", headers={"host": "gigwa-mcp:8184"})

    assert response.status_code != 421


def test_streamable_http_initialize_accepts_json_only_accept_header(monkeypatch) -> None:
    monkeypatch.setenv("GIGWA_MCP_ALLOWED_HOSTS", "testserver,gigwa-mcp")
    configure_http_transport_security()
    _reset_streamable_http_session_manager()

    app = mcp.streamable_http_app()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    }

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "host": "gigwa-mcp:8184",
            },
        )

    assert response.status_code != 406
