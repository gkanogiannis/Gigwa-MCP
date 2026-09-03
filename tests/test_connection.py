"""Unit tests for the runtime connection switch: ``server.set_client`` and the
``gigwa_connect`` tool (verify-then-swap, roll back on failure). Uses a mocked httpx
transport, mirroring tests/test_client.py; no live server."""

from __future__ import annotations

import httpx
import pytest

import gigwa_mcp.server as server
from gigwa_mcp.client import GigwaClient
from gigwa_mcp.config import GigwaConfig
from gigwa_mcp.errors import GigwaAPIError
from gigwa_mcp.tools import connection


def _healthy(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/generateToken"):
        return httpx.Response(201, json={"token": "t"})
    if path.endswith("/swagger-resources"):
        return httpx.Response(200, json=[{"name": "Gigwa API 2.13"}])
    if path.endswith("/userInfo"):
        return httpx.Response(200, json={})
    return httpx.Response(200, json={"Database1": {"database": "demo"}})


def _broken(request: httpx.Request) -> httpx.Response:
    # server_version tolerates errors; the round-trip fails at instanceContentSummary.
    if request.url.path.endswith("/swagger-resources"):
        return httpx.Response(200, json=[])
    return httpx.Response(500, text="boom")


def _mock_client(handler, base_url="http://prev/gigwa") -> GigwaClient:
    client = GigwaClient(GigwaConfig(base_url=base_url))
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _factory(handler, created: list | None = None):
    """A drop-in for ``GigwaClient`` that wires each new client to a mock transport."""
    def make(config: GigwaConfig) -> GigwaClient:
        client = GigwaClient(config)
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        if created is not None:
            created.append(client)
        return client
    return make


# -- set_client -------------------------------------------------------------

def test_set_client_swaps_returns_previous_and_does_not_close(monkeypatch):
    a, b = _mock_client(_healthy), _mock_client(_healthy)
    monkeypatch.setattr(server, "_client", a)
    previous = server.set_client(b)
    assert previous is a
    assert server.get_client() is b
    assert not a._http.is_closed  # caller owns lifecycle; set_client never closes
    server.set_client(None)  # None resets to lazy env construction
    assert server._client is None


# -- gigwa_connect ----------------------------------------------------------

def test_gigwa_connect_switches_verifies_and_closes_old(monkeypatch):
    prev = _mock_client(_healthy)
    monkeypatch.setattr(server, "_client", prev)
    monkeypatch.setattr(connection, "GigwaClient", _factory(_healthy))

    out = connection.gigwa_connect("newhost:8443/gigwa", anonymous=True)

    assert "Switched Gigwa connection" in out
    assert "https://newhost:8443/gigwa" in out
    assert "anonymous" in out
    assert server._client is not prev
    assert server._client.config.base_url == "https://newhost:8443/gigwa"
    assert prev._http.is_closed  # old connection torn down on success


def test_gigwa_connect_rolls_back_on_verify_failure(monkeypatch):
    prev = _mock_client(_healthy)
    created: list[GigwaClient] = []
    monkeypatch.setattr(server, "_client", prev)
    monkeypatch.setattr(connection, "GigwaClient", _factory(_broken, created))

    with pytest.raises(GigwaAPIError):
        connection.gigwa_connect("newhost/gigwa", anonymous=True)

    assert server._client is prev  # restored
    assert not prev._http.is_closed  # previous connection left intact
    assert created and created[0]._http.is_closed  # failed client discarded


def test_gigwa_connect_uses_profile_credentials(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            seen["creds"] = request.read().decode()
            return httpx.Response(201, json={"token": "t"})
        return _healthy(request)

    monkeypatch.setattr(server, "_client", _mock_client(_healthy))
    monkeypatch.setattr(connection, "GigwaClient", _factory(handler))
    monkeypatch.setenv("GIGWA_USER_DEMO", "bob")
    monkeypatch.setenv("GIGWA_PASS_DEMO", "pw")

    out = connection.gigwa_connect("host/gigwa", profile="demo")

    assert "User: bob" in out
    # The password reached Gigwa via generateToken, resolved from env — not a tool arg.
    assert '"bob"' in seen["creds"] and '"pw"' in seen["creds"]


# -- _render_user_info ------------------------------------------------------

def test_render_user_info_is_empty_for_builds_that_report_nothing():
    """Builds returning an empty/absent userInfo must add no lines at all."""
    assert connection._render_user_info({}, "alice") == []
    assert connection._render_user_info(None, "alice") == []
    assert connection._render_user_info([], "alice") == []


def test_render_user_info_reports_identity_only_when_it_adds_information():
    # Same as the configured user -> redundant, omitted.
    assert connection._render_user_info({"user": "alice"}, "alice") == []
    # Different (or unknown locally, e.g. anonymous) -> worth showing.
    assert connection._render_user_info({"user": "alice"}, "bob") == ["Server identity: alice"]
    assert connection._render_user_info({"login": "alice"}, None) == ["Server identity: alice"]


@pytest.mark.parametrize("key", ["authorities", "roles", "permissions"])
def test_render_user_info_renders_roles_under_any_key(key):
    lines = connection._render_user_info({key: ["ROLE_ADMIN", "ROLE_USER"]}, None)
    assert lines == ["Roles: ROLE_ADMIN, ROLE_USER"]


def test_render_user_info_renders_email_permissions_and_admin_flag():
    lines = connection._render_user_info(
        {
            "user": "alice",
            "email": "alice@example.org",
            "authorities": ["ROLE_ADMIN"],
            "writableEntities": ["db1", "db2"],
            "manageableEntities": ["db1"],
            "adminEntities": ["db1"],
            "administrator": True,
        },
        "alice",
    )
    assert lines == [
        "Email: alice@example.org",
        "Roles: ROLE_ADMIN",
        "Writable databases: db1, db2",
        "Manageable databases: db1",
        "Admin databases: db1",
        "Administrator: yes",
    ]


def test_render_user_info_truncates_long_database_lists():
    lines = connection._render_user_info({"writableEntities": [f"db{i}" for i in range(12)]}, None)
    assert lines == ["Writable databases: db0, db1, db2, db3, db4, db5, db6, db7, db8, db9 …"]


def test_render_user_info_accepts_the_admin_alias():
    assert connection._render_user_info({"admin": True}, None) == ["Administrator: yes"]
    # Falsy flags stay silent rather than reporting "no".
    assert connection._render_user_info({"administrator": False, "admin": False}, None) == []


def test_server_info_surfaces_user_info_lines():
    """End-to-end: a populated userInfo reaches the gigwa_server_info summary."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/userInfo"):
            return httpx.Response(
                200, json={"authorities": ["ROLE_ADMIN"], "writableEntities": ["demo"]}
            )
        return _healthy(request)

    out = connection._verify_and_describe(_mock_client(handler))

    assert "Roles: ROLE_ADMIN" in out
    assert "Writable databases: demo" in out
