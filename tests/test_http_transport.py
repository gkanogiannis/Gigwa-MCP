from starlette.testclient import TestClient

from gigwa_mcp.__main__ import configure_http_transport_security
from gigwa_mcp.server import mcp


def test_streamable_http_app_allows_docker_service_host(monkeypatch) -> None:
    monkeypatch.setenv("GIGWA_MCP_ALLOWED_HOSTS", "gigwa-mcp")
    configure_http_transport_security()

    app = mcp.streamable_http_app()
    with TestClient(app) as client:
        response = client.get("/mcp", headers={"host": "gigwa-mcp:8184"})

    assert response.status_code != 421
