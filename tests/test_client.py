"""GigwaClient unit tests: auth, token refresh, multipart upload, progress polling."""

from __future__ import annotations

import httpx
import pytest

from gigwa_mcp.client import GigwaClient
from gigwa_mcp.config import GigwaConfig
from gigwa_mcp.errors import GigwaAPIError, GigwaExportError, GigwaImportError
from gigwa_mcp.exports import BULK_TRANSFER_TIMEOUT, DOWNLOAD_STALL_SECONDS


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


def test_anonymous_sends_no_auth_and_never_generates_token():
    token_calls = {"n": 0}
    saw_auth = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            token_calls["n"] += 1
            return httpx.Response(201, json={"token": "x"})
        if "authorization" in {k.lower() for k in request.headers}:
            saw_auth["n"] += 1
        return httpx.Response(200, json={"ok": True})

    # No credentials configured -> anonymous access.
    client = GigwaClient(GigwaConfig(base_url="http://test/gigwa"))
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.anonymous is True
    assert client.instance_content_summary() == {"ok": True}
    assert token_calls["n"] == 0  # never hit generateToken
    assert saw_auth["n"] == 0  # never sent an Authorization header


def test_anonymous_does_not_retry_on_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = GigwaClient(GigwaConfig(base_url="http://test/gigwa"))
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    resp = client.request("GET", "/gigwa/instanceContentSummary")
    assert resp.status_code == 401  # returned as-is, no auth-refresh loop


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


def test_export_variantset_vcf_raises_promptly_on_reported_error():
    """A failure surfaces in the progress JSON's "error" field, not as a non-202/200
    HTTP status on the main export endpoint -- the main endpoint just keeps returning
    202 forever. Left unread, this would silently poll until the *outer* timeout
    instead of surfacing the real cause -- assert it's read and raised immediately."""
    calls = {"export": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        if request.url.path.endswith("/export/vcf"):
            calls["export"] += 1
            return httpx.Response(202, text="Initiating export...")
        if request.url.path.endswith("/gigwa/progress"):
            return httpx.Response(
                200, json={"error": "Out of memory during export", "complete": False}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = make_client(handler)
    with pytest.raises(GigwaAPIError, match="Out of memory during export"):
        client.export_variantset_vcf("VS§1§run", "/tmp/should-not-be-written.vcf", poll_interval=0, timeout=60)
    # Raised on the first poll -- proves it didn't fall through to the outer timeout.
    assert calls["export"] == 1


def test_export_variantset_vcf_reads_body_with_bulk_transfer_timeout(tmp_path):
    """The GET that reads the completed export's (potentially hundreds-of-MB) body must
    use the long bulk-transfer timeout, not GigwaConfig's ordinary ~120s API timeout --
    confirmed live: a large export failed against the short one while a same-sized,
    percentage-tracked request (small JSON polls only) completed cleanly."""
    seen_timeout = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        if request.url.path.endswith("/export/vcf"):
            seen_timeout["value"] = request.extensions.get("timeout")
            return httpx.Response(200, content=b"##fileformat=VCFv4.1\n" + b"x" * 100)
        raise AssertionError(f"unexpected request: {request.url}")

    client = make_client(handler)
    client.export_variantset_vcf("VS§1§run", tmp_path / "out.vcf", poll_interval=0, timeout=60)
    assert seen_timeout["value"] == {
        "connect": BULK_TRANSFER_TIMEOUT.connect,
        "read": BULK_TRANSFER_TIMEOUT.read,
        "write": BULK_TRANSFER_TIMEOUT.write,
        "pool": BULK_TRANSFER_TIMEOUT.pool,
    }


def test_export_download_uses_long_total_but_short_stall_timeout(tmp_path):
    """ExportManager.download() -- the raw file GET behind fetch_export_file /
    export_selection -- keeps the long bulk allowance overall, but a short per-read
    (inactivity) timeout so a dead connection fails fast instead of hanging 30 min."""
    seen_timeout = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        seen_timeout["value"] = request.extensions.get("timeout")
        return httpx.Response(200, content=b"x" * 100)

    client = make_client(handler)
    client.exports.download("/download/it", tmp_path / "out.vcf")
    assert seen_timeout["value"] == {
        "connect": BULK_TRANSFER_TIMEOUT.connect,
        "read": DOWNLOAD_STALL_SECONDS,
        "write": BULK_TRANSFER_TIMEOUT.write,
        "pool": BULK_TRANSFER_TIMEOUT.pool,
    }


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("timed out"), httpx.RemoteProtocolError("peer closed connection")]
)
def test_export_download_reports_a_stalled_or_dropped_connection_clearly(tmp_path, exc):
    """A connection lost mid-download (e.g. a network switch) must surface as an
    explicit, retryable GigwaExportError -- not a silent hang or a bare httpx error --
    and leave no partial file behind."""

    class Dying(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 1000
            raise exc

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        return httpx.Response(200, stream=Dying())

    client = make_client(handler)
    with pytest.raises(GigwaExportError, match="fetch_export_file"):
        client.exports.download("/download/it", tmp_path / "out.vcf")
    assert list(tmp_path.iterdir()) == []


def test_export_download_reports_progress_during_transfer(tmp_path, monkeypatch):
    """A long download must not go MCP-protocol-silent -- confirmed live: a transfer
    that was still genuinely receiving bytes (just slowly) got killed by the harness's
    own silence watchdog, hours past every timeout in this client, because download()
    never called notify() during the transfer itself. Assert it now does, repeatedly,
    with real growing byte-based percentages -- not just once at the end."""
    from gigwa_mcp import progress as progress_mod

    total_size = 12_000_000  # spans several ~5MB notify thresholds
    body = b"x" * total_size

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        return httpx.Response(200, content=body, headers={"content-length": str(total_size)})

    client = make_client(handler)

    emitted: list[tuple] = []
    monkeypatch.setattr(progress_mod.from_thread, "run", lambda fn, *args: emitted.append(args))
    token = progress_mod.set_reporter(lambda *a: None)
    try:
        result = client.exports.download("/download/it", tmp_path / "out.bin")
    finally:
        progress_mod.reset_reporter(token)

    assert result.stat().st_size == total_size
    assert len(emitted) >= 2, "expected multiple progress pings across a 12MB transfer"
    pcts = [args[0] for args in emitted]
    assert pcts == sorted(pcts)  # monotonically increasing
    assert pcts[-1] == 100.0  # always ends clean, even though the threshold-based
    # mid-transfer pings alone would generally land short of it


def test_export_selection_uses_a_dedicated_token_not_the_shared_session_one(tmp_path):
    """Gigwa tracks one "current export" per token, not per session -- reusing the
    client's shared session token for an export risks colliding with *any* other
    export sharing that token, whether a deliberate concurrent run or just a retry
    issued before a failed attempt's server-side thread actually died. Each export
    must mint and use its own token, and poll progress under that same token."""
    tokens_issued: list[str] = []
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            tokens_issued.append(f"tok{len(tokens_issued)}")
            return httpx.Response(201, json={"token": tokens_issued[-1]})
        if request.url.path.endswith("/gigwa/exportData"):
            seen["export_post_auth"] = request.headers.get("authorization")
            return httpx.Response(200, text="/download/it")  # queued: a download URL
        if request.url.path.endswith("/gigwa/progress"):
            seen["progress_auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"complete": True})
        return httpx.Response(200, content=b"vcf-bytes")  # the actual file download

    client = make_client(handler)
    client._generate_token()  # establish the shared session token first, like normal
    # use -- so we can prove the export's token is a genuinely different one, not just
    # "a token" in the abstract.
    shared_token = client._token

    client.export_selection("VS§1§run", tmp_path / "out.vcf", poll_interval=0)

    export_token = seen["export_post_auth"].removeprefix("Bearer ")
    assert export_token != shared_token  # dedicated, not the shared session token
    # progress was polled under that *same* dedicated token, not the shared one either
    assert seen["progress_auth"] == f"Bearer export_{export_token}"


def test_export_selection_gives_two_calls_two_different_tokens(tmp_path):
    """Two exports -- concurrent, or merely overlapping (a retry started before the
    previous attempt's server-side thread died) -- must never share a token, or Gigwa's
    per-token "current export" tracking conflates them."""
    tokens_issued: list[str] = []
    export_auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            tokens_issued.append(f"tok{len(tokens_issued)}")
            return httpx.Response(201, json={"token": tokens_issued[-1]})
        if request.url.path.endswith("/gigwa/exportData"):
            export_auths.append(request.headers.get("authorization"))
            return httpx.Response(200, text="/download/it")
        if request.url.path.endswith("/gigwa/progress"):
            return httpx.Response(200, json={"complete": True})
        return httpx.Response(200, content=b"vcf-bytes")

    client = make_client(handler)
    client.export_selection("VS§1§run", tmp_path / "a.vcf", poll_interval=0)
    client.export_selection("VS§1§run", tmp_path / "b.vcf", poll_interval=0)

    assert len(export_auths) == 2
    assert export_auths[0] != export_auths[1]


def test_export_selection_anonymous_client_sends_no_export_token(tmp_path):
    """Anonymous access has no credentials to mint a dedicated token with -- must not
    try to authenticate the export POST at all, same as every other anonymous request."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in {k.lower() for k in request.headers}
        if request.url.path.endswith("/gigwa/exportData"):
            return httpx.Response(200, text="/download/it")
        if request.url.path.endswith("/gigwa/progress"):
            return httpx.Response(200, json={"complete": True})
        return httpx.Response(200, content=b"vcf-bytes")

    cfg = GigwaConfig(base_url="http://test/gigwa")  # no username/password -> anonymous
    client = GigwaClient(cfg)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.anonymous
    client.export_selection("VS§1§run", tmp_path / "out.vcf", poll_interval=0)
