"""GigwaClient unit tests: auth, token refresh, multipart upload, progress polling."""

from __future__ import annotations

import httpx
import pytest

from gigwa_mcp.client import GigwaClient
from gigwa_mcp.config import GigwaConfig
from gigwa_mcp.errors import GigwaImportError


def make_client(handler) -> GigwaClient:
    cfg = GigwaConfig(base_url="http://test/gigwa", username="u", password="p")
    client = GigwaClient(cfg)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_generates_token_and_sends_bearer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "abc"})
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    client = make_client(handler)
    client.instance_content_summary()
    assert seen["auth"] == "Bearer abc"


def test_refreshes_token_on_401():
    issued: list[str] = []
    tokens = ["t1", "t2"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            tok = tokens[len(issued)]
            issued.append(tok)
            return httpx.Response(201, json={"token": tok})
        if request.headers.get("authorization") == "Bearer t1":
            return httpx.Response(401, text="expired")
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    assert client.instance_content_summary() == {"ok": True}
    assert issued == ["t1", "t2"]  # initial token, then one refresh


def test_genotype_import_builds_multipart_and_returns_token(tmp_path):
    dart = tmp_path / "x.dart"
    dart.write_text("AlleleID,Chrom_,S1\nm1,Unmapped,0\n")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json="import::u::xyz")

    client = make_client(handler)
    token = client.genotype_import(
        module="M", project="P", run="R", data_files=[str(dart)],
        technology="DArTseq", ploidy=2, skip_monomorphic=True,
    )
    assert token == "import::u::xyz"
    assert "multipart/form-data" in captured["content_type"]
    assert b'name="file[0]"' in captured["body"]
    assert captured["params"]["module"] == "M"
    assert captured["params"]["skipMonomorphic"] == "true"
    assert captured["params"]["ploidy"] == "2"


def test_progress_204_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        return httpx.Response(204)

    client = make_client(handler)
    assert client.progress("tok") is None


def test_wait_for_completion_succeeds():
    seq = [
        httpx.Response(200, json={"complete": False, "progressDescription": "working", "currentStepProgress": 50}),
        httpx.Response(200, json={"complete": True, "progressDescription": "done", "currentStepProgress": 100}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        return seq.pop(0)

    client = make_client(handler)
    final = client.wait_for_completion("tok", poll_interval=0)
    assert final.complete and final.percent == 100


def test_wait_for_completion_raises_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        return httpx.Response(200, json={"error": "Found no data to import!", "complete": False})

    client = make_client(handler)
    with pytest.raises(GigwaImportError):
        client.wait_for_completion("tok", poll_interval=0)
