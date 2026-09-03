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
- ``POST /gigwa/exportData`` (the endpoint the Gigwa web UI itself uses for a
  filtered/selection-based export) returns the eventual download URL as plain text
  immediately, while the export runs server-side; its progress is polled via
  ``GET /gigwa/progress`` with **no** ``progressToken`` param and an ``Authorization:
  Bearer export_<token>`` header instead (see ``GigwaClient._export_progress``).
- ``POST /gigwa/distinctIndividualMetadata/{module}`` and
  ``POST /gigwa/filterIndividualsFromMetadata/{module}`` (the endpoints behind the Gigwa
  web UI's "select individuals by attribute" filters) expose per-individual metadata
  Gigwa's BrAPI ``search/germplasm`` does not reliably populate on every build.
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
from .errors import GigwaAPIError, GigwaAuthError, GigwaExportError, GigwaImportError
from .progress import notify


def _bool(value: bool | None) -> str | None:
    """Render a tri-state boolean as Gigwa expects ("true"/"false"/omitted)."""
    if value is None:
        return None
    return "true" if value else "false"


# Gigwa's GA4GH ``/ga4gh/variants/search`` is stateful: a first POST in COUNT mode returns
# the number of matching variants (and caches their ids server-side under a hash of the
# query), then further POSTs in FETCH mode page through those variants. These are Gigwa's
# GigwaSearchVariantsRequest ``searchMode`` values (verified live against 2.12-RELEASE and
# 2.13-beta2: 0 = count only, 3 = return the variants).
SEARCH_MODE_COUNT = 0
SEARCH_MODE_FETCH = 3


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
        # Bound *connection* establishment separately from the (long) read timeout: reads
        # can legitimately take minutes (VCF export / import), but connecting should be
        # quick. This makes an unreachable/misconfigured Gigwa fail in a few seconds
        # instead of hanging for the full timeout — e.g. when a server is booted without a
        # real Gigwa behind it (placeholder creds in a sandbox), so a probe of a
        # connection-touching tool/resource errors promptly rather than stalling.
        timeout = httpx.Timeout(config.timeout, connect=min(config.timeout, config.connect_timeout))
        self._http = httpx.Client(timeout=timeout, follow_redirects=True)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GigwaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth --------------------------------------------------------------
    @property
    def anonymous(self) -> bool:
        """True when no credentials are configured — requests go out unauthenticated.

        Gigwa treats unauthenticated requests as the *anonymous* user, which can perform
        the public/read-only operations a given instance exposes. (Anonymous access is
        "send no token", not "send an empty token" — ``generateToken`` rejects empty creds.)
        """
        return not (self.config.username and self.config.password)

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
        if self.anonymous:
            return {}  # no Authorization header => anonymous access
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
        headers: dict[str, str] | None = None,
        _retry_auth: bool = True,
    ) -> httpx.Response:
        """Issue one request. ``headers``, when given, replaces the normal auth header
        entirely (used to poll export progress under the ``"export_"``-prefixed pseudo
        token — see :meth:`_export_progress`) and disables the 401 auto-retry, since that
        override isn't the expired-token case the retry exists for."""
        url = f"{self.rest}{path}"
        resp = self._http.request(
            method,
            url,
            params=params,
            files=files,
            json=json_body,
            headers=headers if headers is not None else self._token_header(),
        )
        if resp.status_code == 401 and _retry_auth and not self.anonymous and headers is None:
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

    def _poll_until_complete(
        self,
        poll_fn: Callable[[], "ProgressStatus | None"],
        *,
        poll_interval: float,
        timeout: float,
        what: str,
        error_cls: type[Exception] = GigwaImportError,
    ) -> ProgressStatus:
        """Shared polling loop behind :meth:`wait_for_completion` (imports) and
        :meth:`_wait_for_export` (exports) — same server-side ``ProgressIndicator`` shape,
        different process-id/lookup mechanism per :meth:`progress` vs. :meth:`_export_progress`.

        A 204 (no content) is treated as "finished" only once at least one real status has
        been observed, so we don't mistake a not-yet-started job for a completed one.
        """
        deadline = time.monotonic() + timeout
        last: ProgressStatus | None = None
        seen = False
        while True:
            status = poll_fn()
            if status is not None:
                seen = True
                last = status
                pct = status.percent
                notify(
                    status.summary(),
                    pct if (pct is not None and 0 <= pct <= 100) else None,
                    100,
                )
                if status.error:
                    raise error_cls(f"{what} failed: {status.error}")
                if status.aborted:
                    raise error_cls(f"{what} was aborted on the server.")
                if status.complete:
                    return status
            elif seen:
                # Progress cleared after we saw activity -> the job finished.
                return last if last is not None else ProgressStatus(raw={"complete": True})

            if time.monotonic() >= deadline:
                raise error_cls(
                    f"Timed out after {timeout:.0f}s waiting for {what.lower()} to finish "
                    f"(last status: {last.summary() if last else 'none'})."
                )
            time.sleep(poll_interval)

    def wait_for_completion(
        self,
        token: str,
        *,
        poll_interval: float = 1.5,
        timeout: float = 1800.0,
        on_update: Callable[[ProgressStatus], None] | None = None,
    ) -> ProgressStatus:
        """Poll progress until the job completes, errors, or aborts."""

        def poll() -> ProgressStatus | None:
            status = self.progress(token)
            if status is not None and on_update is not None:
                on_update(status)
            return status

        return self._poll_until_complete(
            poll, poll_interval=poll_interval, timeout=timeout, what="Import"
        )

    # -- export progress (distinct process-id scheme from imports) ---------
    def export_progress(self) -> ProgressStatus | None:
        """Poll the progress of *this session's* most recent export (started via
        :meth:`start_export`/:meth:`export_selection`).

        Gigwa tracks an export under the process id ``"export_" + <session token>``
        (``GigwaGa4ghServiceImpl.exportVariants``: ``processId = "export_" + token``), and
        ``GET /gigwa/progress`` falls back to reading its process id straight from the
        ``Authorization`` header (``tokenManager.readToken(request)``) whenever no
        ``progressToken`` query parameter is given (``GigwaRestController.getProcessProgress``).
        So — unlike an import, which is polled by the token *returned from* the import call —
        an export is polled by re-authenticating with an ``"export_"``-prefixed pseudo-token
        instead of a query parameter, and takes no id: there is exactly one "current export"
        per authenticated session. Verified against the Gigwa server source (not just a
        browser capture): confirms the mechanism is exactly this, not a coincidence of one
        build's URL.
        """
        if not self._token:
            self._generate_token()
        resp = self.request(
            "GET", "/gigwa/progress", headers={"Authorization": f"Bearer export_{self._token}"}
        )
        if resp.status_code == 204:
            return None
        self._check(resp, "progress (export)")
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return ProgressStatus(raw=data)

    def _wait_for_export(self, *, poll_interval: float, timeout: float) -> ProgressStatus:
        return self._poll_until_complete(
            self.export_progress,
            poll_interval=poll_interval,
            timeout=timeout,
            what="Export",
            error_cls=GigwaExportError,
        )

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
            notify("Exporting VCF from Gigwa…")
            time.sleep(poll_interval)

    # -- variant search / filtering (GA4GH) --------------------------------
    @staticmethod
    def _ga4gh_variant_set_id(variant_set_db_id: str) -> str:
        """GA4GH's ``variantSetId`` is ``module§project`` — the BrAPI variantSetDbId with
        its trailing ``§run`` segment dropped. Gigwa's GA4GH endpoints split this id
        expecting exactly module+project and throw a NullPointerException on the full
        three-part id, so we always normalise here."""
        return "§".join(str(variant_set_db_id).split("§")[:2])

    def _variant_search_body(
        self,
        variant_set_db_id: str,
        *,
        reference_name: str | None,
        start: int | None,
        end: int | None,
        min_maf: float | None,
        max_maf: float | None,
        max_missing_data: float | None,
        callset_ids: Sequence[str] | None,
        search_mode: int,
        page_size: int,
        page_token: str,
        get_gt: bool,
    ) -> dict[str, Any]:
        """Assemble a GigwaSearchVariantsRequest body, matching what the Gigwa web UI sends.

        ``callSetIds`` selects the samples and is sent even when empty — ``[]`` means "all
        individuals" and the endpoint rejects an omitted list (HTTP 400). ``reference_name``
        + ``start``/``end`` filter by region server-side.

        The genotype-stat filters (``min_maf``/``max_maf``/``max_missing_data``) are Gigwa
        per-sample-group arrays. When any is set we build a single group (index 0) spanning
        the whole selection and send the full set of one-element companion arrays the
        server's genotype-filter path requires — an absent/short array makes it throw. Two
        details verified against the UI + live server: MAF and missing-data go to Gigwa as
        **percentages** (0–50 / 0–100), so the 0–1 fractions the tools take are scaled by
        100; and ``discriminate`` must be ``[None]`` ("no discrimination") — a numeric value
        makes Gigwa filter the group against itself and return nothing.
        """
        body: dict[str, Any] = {
            "variantSetId": self._ga4gh_variant_set_id(variant_set_db_id),
            "callSetIds": list(callset_ids) if callset_ids else [],
            "searchMode": search_mode,
            "getGT": get_gt,
            "pageSize": page_size,
            "pageToken": page_token,
        }
        if reference_name:
            body["referenceName"] = reference_name
        if start is not None:
            body["start"] = int(start)
        if end is not None:
            body["end"] = int(end)
        if min_maf is not None or max_maf is not None or max_missing_data is not None:
            body.update(
                {
                    "discriminate": [None],
                    "groupName": [""],
                    "gtPattern": [""],
                    "mostSameRatio": ["100"],
                    "minMaf": [float(min_maf) * 100 if min_maf is not None else 0.0],
                    "maxMaf": [float(max_maf) * 100 if max_maf is not None else 50.0],
                    "minMissingData": [0.0],
                    "maxMissingData": [
                        float(max_missing_data) * 100 if max_missing_data is not None else 100.0
                    ],
                    "minHeZ": [0.0],
                    "maxHeZ": [100.0],
                    "annotationFieldThresholds": [{}],
                    "additionalCallSetIds": [],
                }
            )
        return body

    def count_variants(
        self,
        variant_set_db_id: str,
        *,
        reference_name: str | None = None,
        start: int | None = None,
        end: int | None = None,
        min_maf: float | None = None,
        max_maf: float | None = None,
        max_missing_data: float | None = None,
        callset_ids: Sequence[str] | None = None,
    ) -> int:
        """Count variants matching the given filters, server-side (no genotype data pulled)."""
        body = self._variant_search_body(
            variant_set_db_id,
            reference_name=reference_name,
            start=start,
            end=end,
            min_maf=min_maf,
            max_maf=max_maf,
            max_missing_data=max_missing_data,
            callset_ids=callset_ids,
            search_mode=SEARCH_MODE_COUNT,
            page_size=0,
            page_token="",
            get_gt=False,
        )
        resp = self._check(
            self.request("POST", "/ga4gh/variants/search", json_body=body), "variants/search"
        )
        try:
            data = resp.json()
        except ValueError:
            return 0
        return int(data.get("count") or 0)

    def search_variants(
        self,
        variant_set_db_id: str,
        *,
        reference_name: str | None = None,
        start: int | None = None,
        end: int | None = None,
        min_maf: float | None = None,
        max_maf: float | None = None,
        max_missing_data: float | None = None,
        callset_ids: Sequence[str] | None = None,
        max_variants: int = 100000,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return the variants matching the filters (GA4GH Variant dicts; genotypes not fetched).

        Pages through Gigwa's stateful GA4GH search until the match set is exhausted or
        *max_variants* is reached. The initial COUNT-mode call primes the server-side match
        set that the FETCH-mode pages then read. Each item carries at least ``variantDbId``/
        ``id``, ``referenceName``, ``start``, ``referenceBases`` and ``alternateBases``.
        """
        common = dict(
            reference_name=reference_name,
            start=start,
            end=end,
            min_maf=min_maf,
            max_maf=max_maf,
            max_missing_data=max_missing_data,
            callset_ids=callset_ids,
        )
        total = self.count_variants(variant_set_db_id, **common)
        if total <= 0:
            return []

        out: list[dict[str, Any]] = []
        page_token = "0"  # FETCH mode parses the token as an int; it must not be empty
        cap = min(total, max_variants)
        while len(out) < cap:
            body = self._variant_search_body(
                variant_set_db_id,
                search_mode=SEARCH_MODE_FETCH,
                page_size=page_size,
                page_token=page_token,
                get_gt=False,
                **common,
            )
            resp = self._check(
                self.request("POST", "/ga4gh/variants/search", json_body=body),
                "variants/search",
            )
            data = resp.json()
            variants = data.get("variants")
            if variants is None:  # tolerate a BrAPI-style {"result": {"data": [...]}} shape
                variants = (data.get("result") or {}).get("data") or []
            if not variants:
                break
            out.extend(variants)
            page_token = str(data.get("nextPageToken") or "")
            if not page_token:
                break
        return out[:max_variants]

    # -- sequences / references (GA4GH) ------------------------------------
    def list_sequences(self, variant_set_db_id: str) -> list[dict[str, Any]]:
        """List the reference sequences (chromosomes/contigs) of a variant set.

        Uses GA4GH ``POST /ga4gh/references/search``. Each dict carries at least ``name``
        and, when the server provides it, ``length``. Gigwa parses the ``module§project``
        ``variantSetId`` to locate the project and needs the ``module`` as ``referenceSetId``;
        sending only one of them makes it throw, so both are always included.
        """
        module = variant_set_db_id.split("§", 1)[0]
        resp = self._check(
            self.request(
                "POST",
                "/ga4gh/references/search",
                json_body={
                    "variantSetId": self._ga4gh_variant_set_id(variant_set_db_id),
                    "referenceSetId": module,
                },
            ),
            "references/search",
        )
        try:
            data = resp.json()
        except ValueError:
            return []
        refs = data.get("references")
        if refs is None:
            refs = (data.get("result") or {}).get("data") or []
        return refs or []

    # -- multi-format export ----------------------------------------------
    def export_data(
        self,
        variant_set_db_id: str,
        dest_path: str | Path,
        *,
        fmt: str = "VCF",
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> Path:
        """Export a whole variant set in *fmt*.

        The formats a given Gigwa build accepts vary (verified live on 2.13-beta2:
        ``VCF``, ``PLINK`` and ``Flapjack`` work; ``HAPMAP``/``DARWIN`` return HTTP 400
        "Unsupported data format"). VCF goes through the async BrAPI export
        (:meth:`export_variantset_vcf`); other formats use Gigwa's BrAPI per-format export
        endpoint, which is likewise async (HTTP 202 while preparing, 200 once ready). The
        format token is case-insensitive server-side. A 400 raises a clear error naming the
        formats the server advertises for this set (``availableFormats``).
        """
        fmt_up = str(fmt).upper()
        if fmt_up == "VCF":
            return self.export_variantset_vcf(
                variant_set_db_id, dest_path, poll_interval=poll_interval, timeout=timeout
            )
        dest_path = Path(dest_path)
        quoted = urllib.parse.quote(variant_set_db_id, safe="")
        path = f"/brapi/v2/variantsets/{quoted}/export/{fmt}"
        deadline = time.monotonic() + timeout
        while True:
            resp = self.request("GET", path)
            if resp.status_code == 200 and len(resp.content) > 64:
                dest_path.write_bytes(resp.content)
                return dest_path
            if resp.status_code == 400:
                raise GigwaAPIError(
                    f"Gigwa does not support export format '{fmt}' for this variant set. "
                    f"Available on this instance: {', '.join(self._available_formats(variant_set_db_id)) or 'VCF'}.",
                    status_code=400,
                    body=resp.text,
                )
            if resp.status_code not in (200, 202):
                raise GigwaAPIError(
                    f"{fmt} export failed", status_code=resp.status_code, body=resp.text
                )
            if time.monotonic() >= deadline:
                raise GigwaAPIError(
                    f"{fmt} export timed out after {timeout:.0f}s for {variant_set_db_id}."
                )
            notify(f"Exporting {fmt} from Gigwa…")
            time.sleep(poll_interval)

    def get_export_formats(self) -> dict[str, dict[str, str]]:
        """List export formats this Gigwa build's export-handler registry actually offers.

        ``GET /gigwa/exportFormats`` -> format name -> {"desc", "supportedPloidyLevels"
        (``;``-joined, e.g. ``"2"`` for diploid-only or empty for any ploidy),
        "supportedVariantTypes" (``;``-joined, e.g. ``"SNP;INDEL;MIXED"``, empty for any),
        "dataFileExtensions" (``;``-joined)}. This is the server's live registry (which
        format handlers are registered, and whether experimental ones are enabled on this
        instance) rather than a hardcoded list, so it reflects exactly what a given build
        supports and each format's variant-type/ploidy restrictions.
        """
        resp = self._check(self.request("GET", "/gigwa/exportFormats"), "exportFormats")
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def start_export(
        self,
        variant_set_db_id: str,
        *,
        fmt: str = "VCF",
        reference_name: str | None = None,
        start: int | None = None,
        end: int | None = None,
        selected_variant_types: str | None = None,
        min_maf: float | None = None,
        max_maf: float | None = None,
        max_missing_data: float | None = None,
        callset_ids: Sequence[str] | None = None,
        exported_individuals: Sequence[str] | None = None,
        metadata_fields: Sequence[str] | None = None,
        keep_on_server: bool = False,
    ) -> str:
        """Kick off a filtered/selected export and return its eventual download URL
        immediately, *without* waiting for the export to finish — the non-blocking half of
        :meth:`export_selection` (which POSTs this, waits via :meth:`export_progress`, then
        downloads). Pair with :meth:`export_progress` to poll status and
        :meth:`download_export` to retrieve the file once complete.

        Accepts the same region/MAF/missing-data filters as :meth:`count_variants`, plus
        ``selected_variant_types`` (``;``-joined, e.g. ``"SNP;INDEL"`` — see
        :meth:`get_export_formats` for what a given ``fmt`` accepts), a specific
        ``exported_individuals``/``callset_ids`` selection (default: everyone), and
        ``metadata_fields`` (individual metadata columns to embed in the export). ``fmt``
        is any format name the server advertises, matched case-sensitively against its
        export-handler registry — including ``"VCF.gz"`` (bgzipped VCF), which is its own
        registered format, not a flag on plain ``"VCF"``.

        ``POST /gigwa/exportData`` itself returns immediately with this URL as a
        plain-text body — the export runs server-side in a background thread and streams
        to that path — which is what makes the non-blocking split possible.
        ``keep_on_server`` mirrors Gigwa's own "keep in my temp-output area" toggle;
        default False, since an MCP export is normally a one-shot download.
        """
        body = self._variant_search_body(
            variant_set_db_id,
            reference_name=reference_name,
            start=start,
            end=end,
            min_maf=min_maf,
            max_maf=max_maf,
            max_missing_data=max_missing_data,
            callset_ids=callset_ids,
            search_mode=SEARCH_MODE_FETCH,
            page_size=100,
            page_token="0",
            get_gt=False,
        )
        if selected_variant_types:
            body["selectedVariantTypes"] = selected_variant_types
        body["exportFormat"] = fmt
        body["keepExportOnServer"] = bool(keep_on_server)
        body["exportedIndividuals"] = list(exported_individuals) if exported_individuals else []
        body["metadataFields"] = list(metadata_fields) if metadata_fields else []

        resp = self._check(self.request("POST", "/gigwa/exportData", json_body=body), "exportData")
        export_url = resp.text.strip()
        if not export_url:
            raise GigwaAPIError("exportData did not return a download URL.")
        return export_url

    def download_export(self, export_url: str, dest_path: str | Path) -> Path:
        """Fetch a completed export from the URL :meth:`start_export` returned, once
        :meth:`export_progress` reports it complete."""
        split = urllib.parse.urlsplit(self.config.base_url)
        origin = f"{split.scheme}://{split.netloc}"
        download_url = export_url if export_url.startswith("http") else f"{origin}{export_url}"
        dl = self._check(self._http.get(download_url, headers=self._token_header()), "export download")

        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(dl.content)
        return dest_path

    def export_selection(
        self,
        variant_set_db_id: str,
        dest_path: str | Path,
        *,
        fmt: str = "VCF",
        reference_name: str | None = None,
        start: int | None = None,
        end: int | None = None,
        selected_variant_types: str | None = None,
        min_maf: float | None = None,
        max_maf: float | None = None,
        max_missing_data: float | None = None,
        callset_ids: Sequence[str] | None = None,
        exported_individuals: Sequence[str] | None = None,
        metadata_fields: Sequence[str] | None = None,
        keep_on_server: bool = False,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> Path:
        """Export a filtered/selected subset of a variant set and block until it's
        downloaded — :meth:`start_export`, :meth:`_wait_for_export`, :meth:`download_export`
        in one call. See :meth:`start_export` for the filter parameters; use that plus
        :meth:`export_progress`/:meth:`download_export` directly for a non-blocking export.
        """
        export_url = self.start_export(
            variant_set_db_id,
            fmt=fmt,
            reference_name=reference_name,
            start=start,
            end=end,
            selected_variant_types=selected_variant_types,
            min_maf=min_maf,
            max_maf=max_maf,
            max_missing_data=max_missing_data,
            callset_ids=callset_ids,
            exported_individuals=exported_individuals,
            metadata_fields=metadata_fields,
            keep_on_server=keep_on_server,
        )
        notify(f"Exporting {fmt} from Gigwa…")
        self._wait_for_export(poll_interval=poll_interval, timeout=timeout)
        return self.download_export(export_url, dest_path)

    def _available_formats(self, variant_set_db_id: str) -> list[str]:
        """Best-effort list of export formats a variant set advertises (``availableFormats``)."""
        try:
            for vs in self.list_variantsets():
                if vs.get("variantSetDbId") == variant_set_db_id:
                    return [f.get("dataFormat") for f in vs.get("availableFormats", []) if f.get("dataFormat")]
        except Exception:  # noqa: BLE001 - purely advisory
            pass
        return []

    # -- process control / user info --------------------------------------
    def abort(self, token: str) -> bool:
        """Ask Gigwa to abort the process identified by *token*. Returns True on success.

        The endpoint is ``DELETE /gigwa/abortProcess?processID=<token>`` — verified live
        against 2.13-beta2 (GET/POST return HTTP 500 "method not supported"; DELETE
        returns 200 and stops the running job).
        """
        resp = self.request("DELETE", "/gigwa/abortProcess", params={"processID": token})
        if resp.status_code >= 400:
            raise GigwaAPIError("abortProcess failed", status_code=resp.status_code, body=resp.text)
        return True

    def user_info(self) -> dict[str, Any]:
        """Return the current user's info/permissions (``GET /gigwa/userInfo``)."""
        resp = self.request("GET", "/gigwa/userInfo")
        if resp.status_code >= 400:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # -- germplasm metadata (BrAPI) ---------------------------------------
    def distinct_individual_metadata(
        self, module: str, *, individuals: Sequence[str] | None = None
    ) -> dict[str, list[str]]:
        """Discover per-individual metadata field names and their distinct values.

        ``POST /gigwa/distinctIndividualMetadata/{module}`` -- the endpoint the Gigwa web
        UI itself uses to populate its "select individuals by attribute" filters. Distinct
        from (and, on builds where that path returns no ``additionalInfo``, more complete
        than) the BrAPI germplasm search :meth:`get_germplasm` also tries. ``individuals``
        optionally restricts value discovery to that subset (default: the whole module).
        """
        resp = self._check(
            self.request(
                "POST",
                f"/gigwa/distinctIndividualMetadata/{module}",
                json_body={"individuals": list(individuals)} if individuals else {},
            ),
            "distinctIndividualMetadata",
        )
        try:
            data = resp.json()
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): [str(v) for v in (vs or [])] for k, vs in data.items()}

    def filter_individuals_by_metadata(
        self, module: str, filters: dict[str, Sequence[str]]
    ) -> list[dict[str, Any]]:
        """Return individuals whose metadata matches *filters* (field name -> allowed
        values; multiple fields AND together, multiple values for one field OR together).

        ``POST /gigwa/filterIndividualsFromMetadata/{module}`` -- the same endpoint behind
        the Gigwa web UI's "create group from metadata" dialog. Each result is
        ``{"id": <individual name>, "additionalInfo": {field: value, ...}}``. An empty
        ``filters`` dict returns every individual with its full metadata (used by
        :meth:`get_germplasm` as its primary source).
        """
        resp = self._check(
            self.request(
                "POST",
                f"/gigwa/filterIndividualsFromMetadata/{module}",
                json_body={k: list(v) for k, v in filters.items()},
            ),
            "filterIndividualsFromMetadata",
        )
        try:
            data = resp.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def get_germplasm(self, variant_set_db_id: str) -> list[dict[str, Any]]:
        """Fetch server-stored per-individual metadata for a module, as BrAPI-germplasm-
        shaped records (``germplasmName``/``germplasmDbId``/``additionalInfo``).

        Tries :meth:`filter_individuals_by_metadata` (Gigwa's native, always-populated
        metadata endpoint) first, since BrAPI ``search/germplasm`` on some builds returns
        every individual but with **no** ``additionalInfo`` at all (verified live: the
        endpoint responds 200 with real records, just none of the custom fields). Falls
        back to BrAPI ``POST /brapi/v2/search/germplasm`` then ``GET /brapi/v2/germplasm``
        for builds without the native endpoint. Returns an empty list when nothing works.
        """
        module = variant_set_db_id.split("§", 1)[0]
        try:
            native = self.filter_individuals_by_metadata(module, {})
        except (GigwaAPIError, httpx.HTTPError):
            native = []
        if native:
            return [
                {
                    "germplasmName": rec.get("id"),
                    "germplasmDbId": f"{module}§{rec.get('id')}",
                    "additionalInfo": rec.get("additionalInfo") or {},
                }
                for rec in native
            ]

        for method, path, body in (
            ("POST", "/brapi/v2/search/germplasm", {"programDbIds": [module]}),
            ("GET", "/brapi/v2/germplasm", None),
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
        return []
