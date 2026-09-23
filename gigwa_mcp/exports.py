"""Selection-aware export policy, separated from the Gigwa HTTP transport."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

import httpx

from .errors import GigwaAPIError, GigwaExportError
from .progress import notify

if TYPE_CHECKING:
    from .client import GigwaClient

# GigwaConfig.timeout (120s by default) is a *client-wide* read timeout, sized for
# ordinary API calls -- not for the handful of requests that read a genotype export's
# full body in one blocking call (this module's own download(), and the two BrAPI
# export GETs in client.py that read a completed export's body). Those bodies can be
# hundreds of MB, and a slow link can legitimately take longer than 120s to deliver one
# without anything being actually wrong -- confirmed live: a bulk download failed
# against this exact timeout while a same-sized, percentage-tracked export (a
# *different* request, small JSON polls only) completed cleanly end to end. Give
# large-body requests their own, much longer allowance instead.
BULK_TRANSFER_TIMEOUT = httpx.Timeout(1800.0, connect=10.0)

# download() fetches a file Gigwa has *already finished* writing, so the server has
# no reason to go quiet mid-body: a long gap between bytes means the connection is
# dead (e.g. the client switched networks and the socket is still bound to the old
# address), not slow. httpx's read timeout is per-read inactivity, not a total, so a
# short one here fails such a stall fast without capping a slow-but-live transfer.
# Under BULK_TRANSFER_TIMEOUT a dead socket hung silently for 30 minutes, while the
# heartbeat kept reporting it as merely "waiting for more data".
DOWNLOAD_STALL_SECONDS = 120.0
DOWNLOAD_TIMEOUT = httpx.Timeout(1800.0, connect=10.0, read=DOWNLOAD_STALL_SECONDS)


@dataclass(frozen=True)
class ExportSelection:
    """All filters and output options for a selection-aware export."""

    fmt: str = "VCF"
    reference_name: str | None = None
    start: int | None = None
    end: int | None = None
    selected_variant_types: str | None = None
    min_maf: float | None = None
    max_maf: float | None = None
    max_missing_data: float | None = None
    callset_ids: Sequence[str] | None = None
    exported_individuals: Sequence[str] | None = None
    metadata_fields: Sequence[str] | None = None
    keep_on_server: bool = False


@dataclass(frozen=True)
class ExportStartResult:
    """A queued download URL or an export that completed in the initial response."""

    download_url: str | None = None
    completed_path: Path | None = None
    media_type: str | None = None
    # The dedicated token this export was started under (None for anonymous access, or
    # for a synchronous/completed_path result, which needed no progress tracking at
    # all). Gigwa tracks one "current export" per token, not per session -- pass this
    # to export_progress()/_wait_for_export() to check *this* export specifically,
    # never whatever another concurrent or overlapping export left in the shared slot.
    token: str | None = None

    @property
    def completed_immediately(self) -> bool:
        return self.completed_path is not None


def _atomic_write(path: str | Path, content: bytes) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return destination


class ExportService:
    """Interpret Gigwa export responses and enforce safe download policy."""

    def __init__(self, client: GigwaClient):
        self.client = client

    def start(
        self,
        variant_set_db_id: str,
        selection: ExportSelection,
        *,
        immediate_dest_path: str | Path | None = None,
    ) -> ExportStartResult:
        body = self.client._variant_search_body(
            variant_set_db_id,
            reference_name=selection.reference_name,
            start=selection.start,
            end=selection.end,
            min_maf=selection.min_maf,
            max_maf=selection.max_maf,
            max_missing_data=selection.max_missing_data,
            callset_ids=selection.callset_ids,
            search_mode=3,
            page_size=100,
            page_token="0",
            get_gt=False,
        )
        if selection.selected_variant_types:
            body["selectedVariantTypes"] = selection.selected_variant_types
        body.update(
            exportFormat=selection.fmt,
            keepExportOnServer=bool(selection.keep_on_server),
            exportedIndividuals=list(selection.exported_individuals or ()),
            metadataFields=list(selection.metadata_fields or ()),
        )
        # A dedicated token, not the shared session one: Gigwa tracks one "current
        # export" per token, so two exports sharing a token collide the moment they
        # overlap in time -- whether a deliberate concurrent run, or just a retry
        # issued before the previous attempt's server-side thread actually died.
        # Anonymous access has no credentials to mint one with, so it keeps sharing
        # the (tokenless) default -- concurrent anonymous exports were never isolated
        # from each other and this doesn't change that.
        token = None if self.client.anonymous else self.client._request_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client._check(
            self.client.request(
                "POST", "/gigwa/exportData", json_body=body, headers=headers,
                timeout=BULK_TRANSFER_TIMEOUT,
            ),
            "exportData",
        )
        content = response.content
        media_type = response.headers.get("content-type", "").split(";", 1)[0].lower() or None
        disposition = response.headers.get("content-disposition", "").lower()
        looks_binary = (
            content.startswith((b"PK\x03\x04", b"\x1f\x8b"))
            or "attachment" in disposition
            or bool(media_type and not (media_type.startswith("text/") or media_type.endswith("json")))
        )
        if looks_binary:
            if immediate_dest_path is None:
                raise GigwaExportError(
                    "exportData returned the completed file immediately; provide a destination path."
                )
            # Completed synchronously -- nothing was ever tracked server-side to poll,
            # so no token to report (see the ExportStartResult.token docstring).
            return ExportStartResult(
                completed_path=_atomic_write(immediate_dest_path, content), media_type=media_type
            )

        try:
            candidate = content.decode(response.encoding or "utf-8").strip()
        except UnicodeDecodeError as exc:
            raise GigwaExportError("exportData returned an unrecognized binary response.") from exc
        if not candidate:
            raise GigwaAPIError("exportData did not return a download URL.")
        parsed = urllib.parse.urlsplit(candidate)
        if any(char.isspace() for char in candidate) or (not parsed.path and not parsed.netloc):
            raise GigwaExportError("exportData returned malformed text instead of a download URL.")
        self._safe_download_url(candidate)  # validate before returning it to an MCP caller
        return ExportStartResult(download_url=candidate, media_type=media_type, token=token)

    def _safe_download_url(self, export_url: str) -> str:
        split = urllib.parse.urlsplit(self.client.config.base_url)
        origin = f"{split.scheme}://{split.netloc}"
        parsed = urllib.parse.urlsplit(export_url)
        if parsed.scheme or parsed.netloc:
            if (parsed.scheme, parsed.netloc) != (split.scheme, split.netloc):
                raise GigwaExportError(
                    f"Refusing to download an export from '{parsed.scheme}://{parsed.netloc}': "
                    f"it is not the configured Gigwa server ({origin}). The download URL must "
                    "be the one export_genotypes(wait=False) returned."
                )
            return export_url
        if not export_url.startswith("/"):
            export_url = "/" + export_url
        return f"{origin}{export_url}"

    def download(self, export_url: str, dest_path: str | Path) -> Path:
        """Stream the completed export to *dest_path*, reporting real byte progress via
        :func:`notify` as it arrives.

        A single blocking ``.get()`` (the previous approach) sends no MCP progress
        signal for the whole transfer -- confirmed live: a multi-hour download that was
        still genuinely receiving bytes (just slowly) got killed by the *harness's own*
        silence watchdog, independent of and well past this client's own read timeout,
        for want of a single ``notify()`` call during the transfer. Raising the timeout
        alone (``BULK_TRANSFER_TIMEOUT``) only protects against httpx's own read
        timeout; it does nothing for a harness that gives up when a tool goes quiet.
        """
        download_url = self._safe_download_url(export_url)
        destination = Path(dest_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._stream_to(download_url, destination)
        except httpx.TimeoutException as exc:
            raise GigwaExportError(
                f"Export download stalled: no data received for {DOWNLOAD_STALL_SECONDS:.0f}s "
                f"({type(exc).__name__}) -- the connection to Gigwa was probably lost (network "
                f"change, VPN drop, proxy). Retry: fetch_export_file with the "
                f"same download_url, or rerun the tool."
            ) from exc
        except httpx.TransportError as exc:
            raise GigwaExportError(
                f"Export download failed: connection error ({type(exc).__name__}: {exc}). "
                f"Retry: fetch_export_file with the same "
                f"download_url, or rerun the tool."
            ) from exc

    def _stream_to(self, download_url: str, destination: Path) -> Path:
        with self.client._http.stream(
            "GET", download_url, headers=self.client._token_header(), timeout=DOWNLOAD_TIMEOUT
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise GigwaAPIError(
                    "export download failed", status_code=response.status_code, body=response.text
                )
            content_length = response.headers.get("content-length")
            total_bytes = int(content_length) if content_length and content_length.isdigit() else None

            fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            received = 0
            last_byte_at = time.monotonic()
            last_notified = 0
            notify_every = 5 * 1_000_000  # ~5MB between byte-progress pings -- frequent
            # enough that the harness never sees a long silent stretch on a link that's
            # actually delivering data.

            # But a byte threshold alone goes silent right along with a genuinely idle
            # connection -- confirmed live: a transfer went fully quiet (zero new bytes)
            # for ~1800s and got killed by the harness's watchdog, un-helped by the
            # byte-threshold pings above since there was nothing new to report. A
            # heartbeat thread pings on a fixed clock instead, independent of whether
            # any bytes have actually arrived, so a real stall still reads as "waiting,
            # not dead" rather than silence -- and BULK_TRANSFER_TIMEOUT's read timeout
            # remains the real backstop for a connection that's actually gone.
            stop_heartbeat = threading.Event()

            def heartbeat() -> None:
                while not stop_heartbeat.wait(30):
                    mb = received / 1_000_000
                    pct = 100.0 * received / total_bytes if total_bytes else None
                    idle = time.monotonic() - last_byte_at
                    notify(
                        f"Downloading export… {mb:.0f}MB (no data for {idle:.0f}s; gives up "
                        f"after {DOWNLOAD_STALL_SECONDS:.0f}s)"
                        if idle >= 30
                        else f"Downloading export… {mb:.0f}MB",
                        pct,
                        100,
                    )

            hb_thread = threading.Thread(target=heartbeat, daemon=True)
            hb_thread.start()
            try:
                with os.fdopen(fd, "wb") as stream:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        stream.write(chunk)
                        received += len(chunk)
                        last_byte_at = time.monotonic()
                        if received - last_notified >= notify_every:
                            last_notified = received
                            mb = received / 1_000_000
                            if total_bytes:
                                notify(
                                    f"Downloading export… {mb:.0f}MB",
                                    100.0 * received / total_bytes,
                                    100,
                                )
                            else:
                                notify(f"Downloading export… {mb:.0f}MB")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                # Always end on a clean 100% -- the last mid-transfer ping (every
                # notify_every bytes) generally lands short of the true end, since the
                # final partial chunk rarely aligns with the threshold.
                mb = received / 1_000_000
                if total_bytes:
                    notify(f"Downloading export… {mb:.0f}MB", 100.0, 100)
                else:
                    notify(f"Downloading export… {mb:.0f}MB done")
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
            finally:
                stop_heartbeat.set()
                hb_thread.join(timeout=2)
        return destination

    def export_and_wait(
        self,
        variant_set_db_id: str,
        dest_path: str | Path,
        selection: ExportSelection,
        *,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> Path:
        result = self.start(
            variant_set_db_id, selection, immediate_dest_path=dest_path
        )
        if result.completed_path is not None:
            return result.completed_path
        notify(f"Exporting {selection.fmt} from Gigwa…")
        self.client._wait_for_export(poll_interval=poll_interval, timeout=timeout, token=result.token)
        return self.download(result.download_url or "", dest_path)
