"""Selection-aware export policy, separated from the Gigwa HTTP transport."""

from __future__ import annotations

import os
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from .errors import GigwaAPIError, GigwaExportError
from .progress import notify

if TYPE_CHECKING:
    from .client import GigwaClient


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
        response = self.client._check(
            self.client.request("POST", "/gigwa/exportData", json_body=body), "exportData"
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
        return ExportStartResult(download_url=candidate, media_type=media_type)

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
        download_url = self._safe_download_url(export_url)
        response = self.client._check(
            self.client._http.get(download_url, headers=self.client._token_header()),
            "export download",
        )
        return _atomic_write(dest_path, response.content)

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
        self.client._wait_for_export(poll_interval=poll_interval, timeout=timeout)
        return self.download(result.download_url or "", dest_path)
