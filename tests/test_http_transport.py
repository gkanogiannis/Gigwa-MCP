import asyncio
import json

from starlette.testclient import TestClient

from gigwa_mcp.__main__ import configure_http_transport_security, run_http_server
from gigwa_mcp.server import (
    _MAX_NORMALIZE_BODY_BYTES,
    STREAMABLE_HTTP_PATH,
    _normalize_streamable_http_app,
    mcp,
)


def _reset_streamable_http_session_manager() -> None:
    setattr(mcp, "_session_manager", None)


def _drive_normalizer(chunks: list[bytes]) -> bytes:
    """Feed *chunks* as an ASGI request body through the normalize wrapper and return the
    bytes a downstream app actually receives."""
    received: dict[str, bytes] = {}

    async def app(scope, receive, send) -> None:
        buf = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            buf.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        received["body"] = bytes(buf)

    wrapper = _normalize_streamable_http_app(app)
    queue = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(_message) -> None:
        pass

    scope = {"type": "http", "method": "POST", "path": STREAMABLE_HTTP_PATH}
    asyncio.run(wrapper(scope, receive, send))
    return received["body"]


def test_streamable_http_app_allows_docker_service_host(monkeypatch) -> None:
    monkeypatch.setenv("GIGWA_MCP_ALLOWED_HOSTS", "gigwa-mcp")
    security = configure_http_transport_security()
    _reset_streamable_http_session_manager()

    app = mcp.streamable_http_app(transport_security=security)
    with TestClient(app) as client:
        response = client.get("/mcp", headers={"host": "gigwa-mcp:8184"})

    assert response.status_code != 421


def test_streamable_http_initialize_accepts_json_only_accept_header(monkeypatch) -> None:
    monkeypatch.setenv("GIGWA_MCP_ALLOWED_HOSTS", "testserver,gigwa-mcp")
    security = configure_http_transport_security()
    _reset_streamable_http_session_manager()

    app = mcp.streamable_http_app(transport_security=security)
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


def test_streamable_http_accepts_notifications_initialized_with_id(monkeypatch) -> None:
    monkeypatch.setenv("GIGWA_MCP_ALLOWED_HOSTS", "testserver,gigwa-mcp")
    security = configure_http_transport_security()
    _reset_streamable_http_session_manager()

    app = mcp.streamable_http_app(transport_security=security)
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "host": "gigwa-mcp:8184",
    }

    with TestClient(app) as client:
        # A session must exist first: the StreamableHTTP SDK rejects any post-initialize
        # request without an Mcp-Session-Id ("Missing session ID", 400) *before* the ASGI
        # normalization wrapper runs, so establish one via initialize.
        init = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            headers=headers,
        )
        session_id = init.headers.get("mcp-session-id")
        assert session_id

        # A buggy client sends notifications/initialized WITH an illegal ``id`` field; the
        # wrapper strips it so the SDK accepts the notification (202) instead of 400-ing.
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 123,
                "method": "notifications/initialized",
                "params": None,
            },
            headers={**headers, "mcp-session-id": session_id},
        )

    assert response.status_code == 202


def test_normalizer_strips_id_from_small_notification() -> None:
    body = b'{"jsonrpc":"2.0","id":123,"method":"notifications/initialized","params":null}'
    got = _drive_normalizer([body])
    assert json.loads(got) == {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": None,
    }


def test_normalizer_streams_large_body_through_untouched() -> None:
    # A body far larger than the rewrite cap must be forwarded byte-for-byte (not buffered
    # whole, not normalized). Deliver in chunks so the cap trips mid-stream and the
    # remainder is pulled from the original receive.
    chunk = b"A" * 30_000
    chunks = [chunk] * 4  # 120 KB total, well over _MAX_NORMALIZE_BODY_BYTES (64 KB)
    assert len(chunk) * len(chunks) > _MAX_NORMALIZE_BODY_BYTES
    got = _drive_normalizer(chunks)
    assert got == chunk * 4  # full body delivered, unchanged


def test_run_http_server_binds_loopback_by_default(monkeypatch) -> None:
    import uvicorn

    captured: dict = {}
    monkeypatch.delenv("GIGWA_MCP_HOST", raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    run_http_server(0)
    assert captured["host"] == "127.0.0.1"


def test_run_http_server_honors_gigwa_mcp_host(monkeypatch) -> None:
    import uvicorn

    captured: dict = {}
    monkeypatch.setenv("GIGWA_MCP_HOST", "0.0.0.0")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    run_http_server(0)
    assert captured["host"] == "0.0.0.0"
