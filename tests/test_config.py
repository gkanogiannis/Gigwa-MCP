"""Unit tests for connection config: URL normalisation and out-of-band credential
resolution (``resolve_credentials`` / ``GigwaConfig.for_connection``) — the pieces that
let ``gigwa_connect`` switch servers without a secret ever passing through the chat."""

from __future__ import annotations

import os

import pytest

from gigwa_mcp.config import (
    GigwaConfig,
    _normalize_base_url,
    _profile_key,
    resolve_credentials,
)
from gigwa_mcp.errors import GigwaConfigError


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every GIGWA_* variable so credential resolution is deterministic."""
    for key in list(os.environ):
        if key.startswith("GIGWA_"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


# -- URL normalisation ------------------------------------------------------

def test_normalize_bare_host_port_assumes_https():
    assert _normalize_base_url("abc.xyz:123/gigwa") == "https://abc.xyz:123/gigwa"


def test_normalize_preserves_explicit_scheme_and_trims_slash():
    assert _normalize_base_url("http://h:8080/gigwa/") == "http://h:8080/gigwa"


def test_normalize_empty_raises():
    with pytest.raises(GigwaConfigError):
        _normalize_base_url("   ")


def test_profile_key_sanitises():
    assert _profile_key("my-server") == "MY_SERVER"
    assert _profile_key(" demo ") == "DEMO"


# -- credential resolution --------------------------------------------------

def test_resolve_default_anonymous_when_unset(clean_env):
    assert resolve_credentials() == ("", "")


def test_resolve_default_pair(clean_env):
    clean_env.setenv("GIGWA_USER", "alice")
    clean_env.setenv("GIGWA_PASS", "secret")
    assert resolve_credentials() == ("alice", "secret")


def test_resolve_default_half_pair_raises(clean_env):
    clean_env.setenv("GIGWA_USER", "alice")  # no GIGWA_PASS
    with pytest.raises(GigwaConfigError):
        resolve_credentials()


def test_resolve_named_profile(clean_env):
    clean_env.setenv("GIGWA_USER_DEMO", "bob")
    clean_env.setenv("GIGWA_PASS_DEMO", "pw")
    assert resolve_credentials("demo") == ("bob", "pw")


def test_resolve_missing_profile_raises(clean_env):
    with pytest.raises(GigwaConfigError):
        resolve_credentials("nope")


def test_resolve_anonymous_flag_overrides_env(clean_env):
    clean_env.setenv("GIGWA_USER_DEMO", "bob")
    clean_env.setenv("GIGWA_PASS_DEMO", "pw")
    # anonymous wins even when a profile's creds exist — no Authorization is sent.
    assert resolve_credentials("demo", anonymous=True) == ("", "")


# -- for_connection: the full builder used by the tool ----------------------

def test_for_connection_normalises_url_and_reads_profile_creds(clean_env):
    clean_env.setenv("GIGWA_USER_DEMO", "bob")
    clean_env.setenv("GIGWA_PASS_DEMO", "pw")
    cfg = GigwaConfig.for_connection("host:8443/gigwa", profile="demo")
    assert cfg.base_url == "https://host:8443/gigwa"
    assert (cfg.username, cfg.password) == ("bob", "pw")
    assert cfg.rest_url == "https://host:8443/gigwa/rest"


def test_for_connection_anonymous_has_no_creds(clean_env):
    cfg = GigwaConfig.for_connection("https://pub.example/gigwa", anonymous=True)
    assert cfg.username == "" and cfg.password == ""


def test_for_connection_reuses_default_creds_only_for_home_url(clean_env):
    # Default GIGWA_USER/PASS belong to the configured GIGWA_URL.
    clean_env.setenv("GIGWA_URL", "https://home.example/gigwa")
    clean_env.setenv("GIGWA_USER", "alice")
    clean_env.setenv("GIGWA_PASS", "secret")

    # Reconnecting to the home server (even written as a bare host) reuses them.
    home = GigwaConfig.for_connection("home.example/gigwa")
    assert (home.username, home.password) == ("alice", "secret")

    # Switching to a *different* server without a profile connects anonymously —
    # the home credentials are never transmitted to another host.
    other = GigwaConfig.for_connection("https://other.example/gigwa")
    assert other.username == "" and other.password == ""
