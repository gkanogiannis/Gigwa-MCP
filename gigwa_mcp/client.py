"""HTTP client for the Gigwa REST API.

Wraps authentication (token generation + auto-refresh on 401), the genotype and
metadata import endpoints (multipart uploads), and async progress polling. All
endpoint paths are given relative to the REST base (``<GIGWA_URL>/rest``).

Verified against Gigwa 2.12-RELEASE:
- ``POST /gigwa/generateToken`` {username,password} -> {"token": ...}; token is
  sent as ``Authorization: Bearer <token>``.
- ``POST /gigwa/genotypeImport`` (multipart) returns the progress token as a JSON
  string, e.g. "import::<user>::<uuid>"; the import runs asynchronously.
- ``GET /gigwa/progress?progressToken=...`` returns a JSON status object, or
  HTTP 204 when there is nothing to report.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import httpx

from .config import GigwaConfig
from .errors import GigwaAPIError, GigwaAuthError, GigwaImportError


def _bool(value: bool | None) -> str | None:
    """Render a tri-state boolean as Gigwa expects ("true"/"false"/omitted)."""
    if value is None:
        return None
    return "true" if value else "false"


@dataclass
class ProgressStatus:
    """A snapshot of a Gigwa async import job."""

    raw: dict[str, Any]

    @property
    def complete(self) -> bool:
        return bool(self.raw.get("complete"))

    @property
    def aborted(self) -> bool:
        return bool(self.raw.get("aborted"))

    @property
    def error(self) -> str | None:
        err = self.raw.get("error")
        return str(err) if err else None

    @property
    def description(self) -> str:
        return str(self.raw.get("progressDescription") or "").strip()

    @property
    def percent(self) -> int | None:
        val = self.raw.get("currentStepProgress")
        return int(val) if isinstance(val, (int, float)) else None

    def summary(self) -> str:
        parts: list[str] = []
        if self.description:
            parts.append(self.description)
        # currentStepProgress is a percentage for some steps and a raw count for
        # others (the count is already in the description), so only show it as a
        # percentage when it is in the 0-100 range.
        pct = self.percent
        if pct is not None and 0 <= pct <= 100:
            parts.append(f"{pct}%")
        if self.error:
            parts.append(f"error: {self.error}")
        return " | ".join(parts) or "(no progress info)"


class GigwaClient:
    """Synchronous client for one Gigwa instance."""

    def __init__(self, config: GigwaConfig):
        self.config = config
        self.rest = config.rest_url
        self._token: str | None = None
        self._http = httpx.Client(timeout=config.timeout, follow_redirects=True)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GigwaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth --------------------------------------------------------------
    def _generate_token(self) -> str:
        url = f"{self.rest}/gigwa/generateToken"
        try:
            resp = self._http.post(
                url,
                json={"username": self.config.username, "password": self.config.password},
            )
        except httpx.HTTPError as exc:  # network-level failure
            raise GigwaAuthError(f"Could not reach Gigwa at {url}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise GigwaAuthError("Gigwa rejected the supplied credentials.")
        if resp.status_code >= 400:
            raise GigwaAuthError(
                f"Token generation failed (HTTP {resp.status_code}): {resp.text[:300]}"
            )
        try:
            token = resp.json().get("token")
        except ValueError:
            token = None
        if not token:
            raise GigwaAuthError("Gigwa did not return a token.")
        self._token = token
        return token

    def _token_header(self) -> dict[str, str]:
        if not self._token:
            self._generate_token()
        return {"Authorization": f"Bearer {self._token}"}

    # -- generic request with one auth retry -------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        files: Sequence[tuple[str, Any]] | None = None,
        json_body: Any | None = None,
        _retry_auth: bool = True,
    ) -> httpx.Response:
        url = f"{self.rest}{path}"
        resp = self._http.request(
            method,
            url,
            params=params,
            files=files,
            json=json_body,
            headers=self._token_header(),
        )
        if resp.status_code == 401 and _retry_auth:
            # Token likely expired -> refresh once and retry.
            self._token = None
            return self.request(
                method,
                path,
                params=params,
                files=files,
                json_body=json_body,
                _retry_auth=False,
            )
        return resp

    @staticmethod
    def _check(resp: httpx.Response, action: str) -> httpx.Response:
        if resp.status_code >= 400:
            raise GigwaAPIError(action + " failed", status_code=resp.status_code, body=resp.text)
        return resp

    # -- read-only endpoints ----------------------------------------------
    def instance_content_summary(self) -> dict[str, Any]:
        resp = self._check(
            self.request("GET", "/gigwa/instanceContentSummary"),
            "instanceContentSummary",
        )
        try:
            return resp.json()
        except ValueError:
            return {}

    def server_version(self) -> str | None:
        """Best-effort Gigwa version, parsed from the swagger resource list."""
        try:
            resp = self._http.get(f"{self.rest}/swagger-resources")
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            for entry in resp.json():
                name = str(entry.get("name", ""))
                if name.startswith("Gigwa API"):
                    return name.replace("Gigwa API", "").strip()
        except (ValueError, AttributeError, TypeError):
            return None
        return None

    def list_variantsets(self) -> list[dict[str, Any]]:
        """List every variant set (run) hosted on the instance.

        Each dict carries at least ``variantSetDbId`` and, when the server provides
        them, ``variantSetName``/``variantCount``/``callSetCount``. Tries the BrAPI
        ``search/variantsets`` POST first, then the GET listing, and finally derives
        ids from ``instanceContentSummary`` (``<module>§<projNum>§<run>``) — the
        2.12 build 404s some BrAPI GET listings, so the fallback keeps this working.
        """
        for method, path, body in (
            ("POST", "/brapi/v2/search/variantsets", {}),
            ("GET", "/brapi/v2/variantsets", None),
        ):
            try:
                resp = self.request(method, path, json_body=body)
                if resp.status_code >= 400:
                    continue
                data = (resp.json().get("result") or {}).get("data") or []
            except (httpx.HTTPError, ValueError):
                continue
            if data:
                return data
        return self._variantsets_from_summary()

    def _variantsets_from_summary(self) -> list[dict[str, Any]]:
        """Derive variant-set ids from ``instanceContentSummary`` as a fallback."""
        out: list[dict[str, Any]] = []
        for db in self.instance_content_summary().values():
            if not isinstance(db, dict):
                continue
            module = db.get("database")
            if not module:
                continue
            for key, proj in db.items():
                if not (isinstance(proj, dict) and key.lower().startswith("project")):
                    continue
                m = re.search(r"(\d+)", key)
                proj_num = m.group(1) if m else "1"
                for run in proj.get("runs") or []:
                    out.append(
                        {
                            "variantSetDbId": f"{module}§{proj_num}§{run}",
                            "variantSetName": f"{proj.get('name', module)}§{run}",
                            "variantCount": db.get("markers"),
                            "callSetCount": proj.get("samples") or db.get("individuals"),
                        }
                    )
        return out

    # -- imports -----------------------------------------------------------
    def genotype_import(
        self,
        *,
        module: str,
        project: str,
        run: str,
        data_files: Iterable[str | Path],
        technology: str | None = None,
        ploidy: int | None = None,
        skip_monomorphic: bool | None = None,
        clear_project_data: bool | None = None,
        assembly_name: str | None = None,
    ) -> str:
        """Upload genotype file(s) and start an async import. Returns the progress token."""
        params: dict[str, Any] = {"module": module, "project": project, "run": run}
        if technology:
            params["technology"] = technology
        if ploidy is not None:
            params["ploidy"] = ploidy
        if skip_monomorphic is not None:
            params["skipMonomorphic"] = _bool(skip_monomorphic)
        if clear_project_data is not None:
            params["clearProjectData"] = _bool(clear_project_data)
        if assembly_name is not None:
            params["assemblyName"] = assembly_name

        token = self._upload(
            "/gigwa/genotypeImport", params, data_files, action="genotypeImport"
        )
        if not isinstance(token, str) or not token:
            raise GigwaImportError(f"genotypeImport returned an unexpected response: {token!r}")
        return token

    def metadata_validation(
        self, *, module: str, file_path: str | Path, metadata_type: str = "individual"
    ) -> Any:
        """Validate a metadata file; returns the server's validation result (usually a list)."""
        params = {"moduleExistingMD": module, "metadataType": metadata_type}
        return self._upload(
            "/gigwa/metadataValidation", params, [file_path], action="metadataValidation"
        )

    def metadata_import(
        self, *, module: str, file_path: str | Path, metadata_type: str = "individual"
    ) -> Any:
        """Import a metadata file into an existing module. Returns the server response."""
        params = {"moduleExistingMD": module, "metadataType": metadata_type}
        return self._upload(
            "/gigwa/metadataImport", params, [file_path], action="metadataImport"
        )

    def _upload(
        self,
        path: str,
        params: dict[str, Any],
        file_paths: Iterable[str | Path],
        *,
        action: str,
    ) -> Any:
        """Multipart upload helper. Files are sent as file[0], file[1], ... and closed afterwards."""
        opened: list[Any] = []
        files: list[tuple[str, Any]] = []
        try:
            for idx, fp in enumerate(file_paths):
                p = Path(fp)
                if not p.is_file():
                    raise GigwaAPIError(f"{action}: file not found: {p}")
                handle = p.open("rb")
                opened.append(handle)
                files.append((f"file[{idx}]", (p.name, handle, "application/octet-stream")))
            resp = self._check(
                self.request("POST", path, params=params, files=files), action
            )
        finally:
            for handle in opened:
                handle.close()
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # -- progress ----------------------------------------------------------
    def progress(self, token: str) -> ProgressStatus | None:
        resp = self.request("GET", "/gigwa/progress", params={"progressToken": token})
        if resp.status_code == 204:
            return None
        self._check(resp, "progress")
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return ProgressStatus(raw=data)

    def wait_for_completion(
        self,
        token: str,
        *,
        poll_interval: float = 1.5,
        timeout: float = 1800.0,
        on_update: Callable[[ProgressStatus], None] | None = None,
    ) -> ProgressStatus:
        """Poll progress until the job completes, errors, or aborts.

        A 204 (no content) is treated as "finished" only once at least one real
        status has been observed, so we don't mistake a not-yet-started job for a
        completed one.
        """
        deadline = time.monotonic() + timeout
        last: ProgressStatus | None = None
        seen = False
        while True:
            status = self.progress(token)
            if status is not None:
                seen = True
                last = status
                if on_update is not None:
                    on_update(status)
                if status.error:
                    raise GigwaImportError(f"Import failed: {status.error}")
                if status.aborted:
                    raise GigwaImportError("Import was aborted on the server.")
                if status.complete:
                    return status
            elif seen:
                # Progress cleared after we saw activity -> the job finished.
                return last if last is not None else ProgressStatus(raw={"complete": True})

            if time.monotonic() >= deadline:
                raise GigwaImportError(
                    f"Timed out after {timeout:.0f}s waiting for import to finish "
                    f"(last status: {last.summary() if last else 'none'})."
                )
            time.sleep(poll_interval)

    # -- data access (Phase 2) --------------------------------------------
    def search_callsets(self, variant_set_db_id: str) -> list[dict[str, Any]]:
        """Return the callsets of a variant set (maps callSetDbId -> callSetName etc.)."""
        resp = self._check(
            self.request(
                "POST",
                "/brapi/v2/search/callsets",
                json_body={"variantSetDbIds": [variant_set_db_id]},
            ),
            "search/callsets",
        )
        return resp.json().get("result", {}).get("data", []) or []

    def search_allelematrix(
        self,
        variant_set_db_id: str,
        *,
        variant_page: int = 0,
        variant_page_size: int = 5000,
        callset_page: int = 0,
        callset_page_size: int = 100000,
        data_matrix_abbreviations: Sequence[str] = ("GT",),
    ) -> dict[str, Any]:
        """Fetch one page of the genotype matrix via BrAPI ``search/allelematrix``.

        Returns the raw ``result`` object: ``dataMatrices`` (the GT matrix is a 2-D
        ``variants × callsets`` token array under ``dataMatrices[0]['dataMatrix']``),
        ``variantDbIds``, ``callSetDbIds``, ``pagination`` (a per-dimension list with
        ``totalPages``/``totalCount``) and the genotype separators (``sepUnphased``,
        ``sepPhased``, ``unknownString``). Gigwa expects ``dataMatrixAbbreviations``
        (NOT ``dataMatrixNames``).
        """
        body = {
            "variantSetDbIds": [variant_set_db_id],
            "dataMatrixAbbreviations": list(data_matrix_abbreviations),
            "pagination": [
                {"dimension": "variants", "page": variant_page, "pageSize": variant_page_size},
                {"dimension": "callsets", "page": callset_page, "pageSize": callset_page_size},
            ],
        }
        resp = self._check(
            self.request("POST", "/brapi/v2/search/allelematrix", json_body=body),
            "search/allelematrix",
        )
        return resp.json().get("result", {}) or {}

    def export_variantset_vcf(
        self,
        variant_set_db_id: str,
        dest_path: str | Path,
        *,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> Path:
        """Export a variant set to VCF and save it to *dest_path*.

        Gigwa's BrAPI export is asynchronous: the first call returns HTTP 202
        ("Initiating export..."); we re-request until it returns HTTP 200 with the
        full VCF body, then stream it to disk.
        """
        dest_path = Path(dest_path)
        quoted = urllib.parse.quote(variant_set_db_id, safe="")
        path = f"/brapi/v2/variantsets/{quoted}/export/vcf"
        deadline = time.monotonic() + timeout
        while True:
            resp = self.request("GET", path)
            if resp.status_code == 200 and len(resp.content) > 64:
                dest_path.write_bytes(resp.content)
                return dest_path
            if resp.status_code not in (200, 202):
                raise GigwaAPIError(
                    "VCF export failed", status_code=resp.status_code, body=resp.text
                )
            if time.monotonic() >= deadline:
                raise GigwaAPIError(
                    f"VCF export timed out after {timeout:.0f}s for {variant_set_db_id}."
                )
            time.sleep(poll_interval)
