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


def test_render_summary_prints_exact_variant_set_db_id():
    # Reproduces the shape that caused a real mistake: a human-readable project *name*
    # ("refNB") that is NOT the id segment ("1") — list_content must print the real id
    # per run so a caller never has to guess/assemble one by hand.
    summary = {
        "Database1": {
            "database": "DIVRICE_NB",
            "individuals": 497,
            "markers": 5391401,
            "Project1": {
                "name": "refNB",
                "variantType": ["SNP"],
                "ploidy": 2,
                "samples": 497,
                "runs": ["03052022"],
            },
        }
    }
    out = connection._render_summary(summary)
    assert "project 'refNB'" in out
    assert "variant_set_db_id: DIVRICE_NB§1§03052022" in out
    assert "refNB§1§03052022" not in out  # the name must never leak into the id segment
