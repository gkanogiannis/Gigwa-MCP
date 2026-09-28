"""Session registry of named groups and variant subsets.

Gigwa itself stores neither: its web UI keeps groups as selections inside each query,
and the result of a genotype-level search lives in a **temporary variant collection
bound to the searching token** (``MongoTemplateManager.getTemporaryVariantCollection(
module, token)``) -- one per token, overwritten by that token's next search. So:

* a *group* here is just a named, validated member list (nothing is written to Gigwa);
* a *subset* is a :class:`~gigwa_mcp.selection.VariantSelection` plus a token of its
  own (:meth:`GigwaClient.mint_token`). Its search runs once under that token, after
  which browsing (``searchMode`` 2), exporting and Gigwa's per-interval statistics all
  read the same temporary collection -- several subsets coexist because each has its
  own token, and nothing is recomputed or downloaded to answer "the subset I just made".

Tokens expire with Gigwa's session timeout. Because the full query is kept, an operation
that finds its token rejected (HTTP 401) or its temporary records gone re-mints a token,
re-runs the search and retries once (:func:`run_with_subset`).

State is per process and per Gigwa server: entries remember the server they were made
on and are refused after ``gigwa_connect`` switches to another one.

Releasing server resources. Gigwa drops a token's temporary collection by itself only
when the token expires (or on logout / webapp restart); its web UI also calls
``/gigwa/dropTempCol`` when the user switches database or leaves the page. This server
does the equivalent (:func:`discard_subset`, which also clears the subset's token) when
it knows a collection is no longer needed: a subset is redefined under the same name or
explicitly dropped (``drop_subset``), ``gigwa_connect`` moves to another server, or the
process shuts down. Never while the collection may still be read: a subset whose search
is still running (``wait=False``) or that another tool call is using is released as soon
as that finishes; one with a non-waiting export still running server-side is left to
Gigwa's own expiry cleanup.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TypeVar

from .client import GigwaClient
from .errors import GigwaAPIError, GigwaError
from .progress import notify
from .selection import (
    ENTITIES,
    ENTITY_SAMPLES,
    GroupFilter,
    VariantSelection,
    discrimination_indexes,
    ga4gh_variant_set_id,
    group_filter_from_dict,
)

T = TypeVar("T")
log = logging.getLogger(__name__)

# Upper bound for one dropTempCol call made while shutting down or switching servers.
RELEASE_TIMEOUT = 10.0
# How long a forced release waits for an aborted search to actually stop writing.
ABORT_GRACE = 5.0

# Text Gigwa records when its per-interval statistics find no temporary records
# (MgdbDao.MESSAGE_TEMP_RECORDS_NOT_FOUND: "Unable to find temporary records: please
# SEARCH again!").
_TEMP_RECORDS_GONE = "temporary records"


@dataclass(frozen=True)
class GroupDefinition:
    name: str
    variant_set_db_id: str
    entity: str
    members: tuple[str, ...]
    source: str
    server: str

    @property
    def project(self) -> str:
        return ga4gh_variant_set_id(self.variant_set_db_id)


@dataclass
class Subset:
    name: str
    selection: VariantSelection
    token: str | None
    count: int | None  # None while the search is still running (wait=False)
    server: str
    group_names: tuple[str, ...] = ()
    # Background search started by define_variant_subset(wait=False), and how it ended.
    job: threading.Thread | None = field(default=None, repr=False, compare=False)
    failure: BaseException | None = field(default=None, repr=False, compare=False)
    # Lifecycle: tool calls currently reading the collection, a non-waiting export
    # started from it, and whether it was discarded (then released once idle).
    users: int = field(default=0, repr=False, compare=False)
    async_export: bool = field(default=False, repr=False, compare=False)
    retired: bool = field(default=False, repr=False, compare=False)
    released: bool = field(default=False, repr=False, compare=False)
    release_client: GigwaClient | None = field(default=None, repr=False, compare=False)

    @property
    def running(self) -> bool:
        return self.job is not None and self.job.is_alive()

    @property
    def module(self) -> str:
        return self.selection.module

    @property
    def fingerprint(self) -> str:
        """Stable digest of the query (not the token): identifies cached exports."""
        return hashlib.sha1(repr(self.selection).encode()).hexdigest()[:12]


@dataclass
class _Registry:
    groups: dict[str, GroupDefinition] = field(default_factory=dict)
    subsets: dict[str, Subset] = field(default_factory=dict)
    # Reentrant: the SIGTERM shutdown hook runs on the main thread, which may already hold
    # it (plain tools run there in stdio mode) -- a plain Lock would deadlock the exit.
    lock: threading.RLock = field(default_factory=threading.RLock)


_REGISTRY = _Registry()


def clear() -> None:
    """Forget every group and subset (tests; a server switch makes them unusable anyway)."""
    with _REGISTRY.lock:
        _REGISTRY.groups.clear()
        _REGISTRY.subsets.clear()


def _server(client: GigwaClient) -> str:
    return str(client.config.base_url)


def _check_entity(entity: str) -> str:
    if entity not in ENTITIES:
        raise ValueError(f"entity must be one of {list(ENTITIES)}, got {entity!r}.")
    return entity


# --------------------------------------------------------------------------- groups
def store_group(
    client: GigwaClient,
    *,
    name: str,
    variant_set_db_id: str,
    entity: str,
    members: Iterable[str],
    source: str,
) -> tuple[GroupDefinition, bool]:
    """Register (or replace) a group; returns ``(group, replaced_existing)``."""
    name = str(name).strip()
    if not name:
        raise ValueError("A group needs a non-empty name.")
    group = GroupDefinition(
        name=name,
        variant_set_db_id=variant_set_db_id,
        entity=_check_entity(entity),
        members=tuple(dict.fromkeys(str(m) for m in members)),
        source=source,
        server=_server(client),
    )
    with _REGISTRY.lock:
        replaced = name in _REGISTRY.groups
        _REGISTRY.groups[name] = group
    return group, replaced


def get_group(client: GigwaClient, name: str) -> GroupDefinition:
    group = _REGISTRY.groups.get(str(name))
    if group is None:
        known = ", ".join(sorted(_REGISTRY.groups)) or "none defined yet"
        raise GigwaError(f"Unknown group '{name}' (known groups: {known}). Create it with define_group.")
    if group.server != _server(client):
        raise GigwaError(
            f"Group '{name}' was defined on {group.server}, not on the currently connected "
            "server; define it again here."
        )
    return group


def list_groups() -> list[GroupDefinition]:
    return list(_REGISTRY.groups.values())


def resolve_group_filters(
    client: GigwaClient,
    variant_set_db_id: str,
    specs: list[dict[str, Any]],
    *,
    entity: str | None = None,
) -> tuple[tuple[GroupFilter, ...], bool, tuple[str, ...]]:
    """Turn ``group_filters`` JSON entries into :class:`GroupFilter`\\ s.

    Each entry names a registered ``group`` *or* lists ``members`` inline (or neither:
    the whole project), plus 0-1 thresholds. Returns ``(groups, work_with_samples,
    registered_group_names)``. All groups of one query must be of the same entity
    (individuals or samples) and belong to the queried project.
    """
    if not isinstance(specs, list) or not specs:
        raise ValueError("group_filters must be a non-empty JSON list of group filter objects.")
    project = ga4gh_variant_set_id(variant_set_db_id)
    entities: set[str] = set()
    groups: list[GroupFilter] = []
    names: list[str] = []
    for index, spec in enumerate(specs, 1):
        if not isinstance(spec, dict):
            raise ValueError(f"group_filters entry #{index} must be a JSON object.")
        if spec.get("group") and spec.get("members"):
            raise ValueError(f"group_filters entry #{index}: give either 'group' or 'members', not both.")
        if spec.get("group"):
            group = get_group(client, spec["group"])
            if group.project != project:
                raise ValueError(
                    f"Group '{group.name}' belongs to {group.project}, not to {project}."
                )
            entities.add(group.entity)
            names.append(group.name)
            groups.append(group_filter_from_dict(spec, members=group.members, name=group.name))
        else:
            members = spec.get("members") or []
            if isinstance(members, str):
                members = [m.strip() for m in members.split(",") if m.strip()]
            if members:
                entities.add(_check_entity(entity or "individuals"))
            label = str(spec.get("name") or f"Group{index}")
            groups.append(group_filter_from_dict(spec, members=members, name=label))
    if entity is not None:
        entities.add(_check_entity(entity))
    if len(entities) > 1:
        raise ValueError(
            "All groups of one query must be of the same kind (individuals or samples); "
            f"got {sorted(entities)}."
        )
    discrimination_indexes(groups)  # fail now on an unknown/self 'discriminate_with'
    return tuple(groups), entities == {ENTITY_SAMPLES}, tuple(names)


# -------------------------------------------------------------------------- subsets
def _materialize(client: GigwaClient, selection: VariantSelection) -> tuple[str | None, int]:
    token = client.mint_token()
    count = client.materialize_selection(selection, token=token)
    return token, count


def _next_name() -> str:
    n = len(_REGISTRY.subsets) + 1
    while f"S{n}" in _REGISTRY.subsets:
        n += 1
    return f"S{n}"


def create_subset(
    client: GigwaClient,
    selection: VariantSelection,
    *,
    name: str | None = None,
    group_names: Iterable[str] = (),
    wait: bool = True,
) -> tuple[Subset, bool]:
    """Run *selection* under a fresh token and register it; returns ``(subset, replaced)``.

    ``wait=True`` blocks while Gigwa filters, relaying its progress. ``wait=False`` --
    like ``export_genotypes(wait=False)`` -- registers the subset at once and runs the
    search on a background thread; :func:`subset_status` reports its progress, and any
    later use of the subset waits for it (again relaying progress) before proceeding.
    """
    with _REGISTRY.lock:
        name = str(name) if name else _next_name()
    if wait:
        token, count = _materialize(client, selection)
        subset = Subset(
            name=name, selection=selection, token=token, count=count,
            server=_server(client), group_names=tuple(group_names),
        )
    else:
        subset = Subset(
            name=name, selection=selection, token=client.mint_token(), count=None,
            server=_server(client), group_names=tuple(group_names),
        )

        def search() -> None:
            try:
                subset.count = client.materialize_selection(
                    selection, token=subset.token, track=False
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced by subset_status / ensure_ready
                subset.failure = exc
            finally:
                subset.job = None  # done: `running` must not see this very thread as busy
                if subset.retired:  # discarded while its search was running
                    _release_if_idle(subset)

        subset.job = threading.Thread(target=search, name=f"gigwa-subset-{name}", daemon=True)
    with _REGISTRY.lock:
        previous = _REGISTRY.subsets.get(subset.name)
        _REGISTRY.subsets[subset.name] = subset
    if subset.job is not None:
        subset.job.start()
    if previous is not None and previous is not subset:
        discard_subset(client, previous)  # its collection would otherwise linger until expiry
    return subset, previous is not None


def ensure_ready(client: GigwaClient, subset: Subset) -> Subset:
    """Wait for a background subset search to finish (relaying its progress), then raise
    its failure, if it failed."""
    job = subset.job
    if job is not None and job.is_alive():
        client.follow_search_progress(
            job, token=subset.token,
            what=f"Waiting for subset '{subset.name}': finding matching variants",
        )
    if subset.failure is not None:
        raise GigwaError(
            f"Subset '{subset.name}' could not be built: {subset.failure}. Define it again."
        ) from subset.failure
    return subset


def subset_status(client: GigwaClient, subset: Subset) -> str:
    """One-line status of a subset: still running (with Gigwa's progress), failed, or ready."""
    if subset.running:
        status = client.search_progress(subset.token)
        detail = status.summary() if status is not None and status.summary() else "waiting for Gigwa"
        return f"Subset '{subset.name}' is still being computed by Gigwa: {detail}"
    if subset.failure is not None:
        return f"Subset '{subset.name}' failed: {subset.failure}"
    return f"Subset '{subset.name}' is ready: {subset.count} variant(s)."


def get_subset(
    client: GigwaClient, name: str, variant_set_db_id: str | None = None, *, wait: bool = True
) -> Subset:
    """Look a subset up; by default wait until its (background) search has finished."""
    subset = _REGISTRY.subsets.get(str(name))
    if subset is None:
        known = ", ".join(sorted(_REGISTRY.subsets)) or "none defined yet"
        raise GigwaError(
            f"Unknown subset '{name}' (known subsets: {known}). Create it with define_variant_subset."
        )
    if subset.server != _server(client):
        raise GigwaError(
            f"Subset '{name}' was built on {subset.server}, not on the currently connected "
            "server; define it again here."
        )
    if variant_set_db_id is not None and ga4gh_variant_set_id(variant_set_db_id) != ga4gh_variant_set_id(
        subset.selection.variant_set_db_id
    ):
        raise ValueError(
            f"Subset '{name}' was built on {subset.selection.variant_set_db_id}, "
            f"not on {variant_set_db_id}."
        )
    return ensure_ready(client, subset) if wait else subset


def list_subsets() -> list[Subset]:
    return list(_REGISTRY.subsets.values())


def refresh_subset(client: GigwaClient, subset: Subset) -> Subset:
    """Re-mint the subset's token and re-run its search (expired token / lost records)."""
    if subset.retired:
        raise GigwaError(f"Subset '{subset.name}' was dropped; define it again to use it.")
    old_token = subset.token
    token, count = _materialize(client, subset.selection)
    subset.token, subset.count, subset.job, subset.failure = token, count, None, None
    if old_token:  # whatever is left under the replaced token is now unreachable
        _drop_quietly(client, subset.module, old_token)
    return subset


def _is_stale(exc: Exception) -> bool:
    if isinstance(exc, GigwaAPIError) and exc.status_code == 401:
        return True
    return _TEMP_RECORDS_GONE in str(exc).lower()


def run_with_subset(client: GigwaClient, subset: Subset, operation: Callable[[str | None], T]) -> T:
    """Run ``operation(token)``; if Gigwa says the token or its temporary records are gone,
    rebuild the subset once and retry."""
    with _REGISTRY.lock:
        subset.users += 1
    try:
        try:
            return operation(subset.token)
        except GigwaError as exc:
            if not _is_stale(exc):
                raise
        notify(f"Subset '{subset.name}' expired on the server; searching again…")
        refresh_subset(client, subset)
        return operation(subset.token)
    finally:
        with _REGISTRY.lock:
            subset.users -= 1
        if subset.retired:
            _release_if_idle(subset)


# ------------------------------------------------------------------ releasing resources
def _drop_quietly(
    client: GigwaClient, module: str, token: str, timeout: float | None = None,
    *, clear_token: bool = True,
) -> str | None:
    """dropTempCol (+ clearToken) for *token*; returns an error text instead of raising."""
    try:
        client.drop_temp_collection(module, token=token, clear_token=clear_token, timeout=timeout)
        return None
    except Exception as exc:  # noqa: BLE001 - releasing is best-effort; Gigwa expires it anyway
        log.debug("dropTempCol failed for %s: %s", module, exc)
        return str(exc) or type(exc).__name__


def _export_still_running(subset: Subset) -> bool:
    """Whether a non-waiting export started from this subset is still reading its
    collection on the server (the Gigwa UI likewise never drops during an export)."""
    if not subset.async_export or subset.release_client is None:
        return False
    try:
        status = subset.release_client.export_progress(token=subset.token)
    except Exception:  # noqa: BLE001 - unknown: assume it may still run
        return True
    return status is not None and not (status.complete or status.error or status.aborted)


def _release_if_idle(subset: Subset, timeout: float | None = None, *, force: bool = False) -> str:
    """Release a retired subset's server resources unless something still reads them.

    ``force`` (shutdown) does not wait for tool calls or a background search -- they die
    with the process anyway; a running background search is aborted on the server first.
    A running non-waiting export is still never cut short.
    """
    with _REGISTRY.lock:
        if subset.released:
            return "released"
        if not force and (subset.running or subset.users > 0):
            return "deferred"
    if _export_still_running(subset):  # network call: outside the lock
        return "export-running"
    with _REGISTRY.lock:
        if subset.released:
            return "released"
        if not force and (subset.running or subset.users > 0):
            return "deferred"
        subset.released = True
        job = subset.job
        aborting = job is not None and job.is_alive()
    if aborting and subset.token and subset.release_client is not None:
        try:
            subset.release_client.abort(subset.token, timeout=timeout)  # stop Gigwa filling it
        except Exception:  # noqa: BLE001 - best-effort
            pass
        job.join(ABORT_GRACE)  # the search request returns once Gigwa has stopped writing
    if not subset.token or subset.release_client is None:
        return "released"  # tokenless (anonymous fallback): nothing of ours to drop
    # After cutting a search short, keep the token: should Gigwa still write a last chunk
    # after the drop (recreating the collection), the token's expiry cleanup drops it --
    # a cleared token would leave it behind until Gigwa restarts (seen live).
    error = _drop_quietly(
        subset.release_client, subset.module, subset.token, timeout,
        clear_token=not (aborting and job.is_alive()),
    )
    if error is None:
        log.info("Released Gigwa temporary collection and token of subset '%s'", subset.name)
        return "released"
    return f"failed: {error}"


def discard_subset(
    client: GigwaClient, subset: Subset, *, timeout: float | None = None, force: bool = False
) -> str:
    """Forget a subset and release its temporary collection and token on the server.

    Returns ``"released"``, ``"deferred"`` (a search or another tool call is still using
    it: released as soon as that ends), ``"export-running"`` (a non-waiting export still
    reads it: left to Gigwa's expiry cleanup) or ``"failed: <reason>"``.
    """
    with _REGISTRY.lock:
        if _REGISTRY.subsets.get(subset.name) is subset:
            del _REGISTRY.subsets[subset.name]
        subset.retired = True
        subset.release_client = subset.release_client or client
    return _release_if_idle(subset, timeout, force=force)


def discard_server_subsets(
    client: GigwaClient, *, timeout: float | None = RELEASE_TIMEOUT, force: bool = False
) -> dict[str, str]:
    """Discard every subset built on *client*'s server (server switch, shutdown with
    ``force``)."""
    server = _server(client)
    with _REGISTRY.lock:
        mine = [s for s in _REGISTRY.subsets.values() if s.server == server]
    return {s.name: discard_subset(client, s, timeout=timeout, force=force) for s in mine}


def browse_subset(
    client: GigwaClient,
    subset: Subset,
    *,
    first_page: int = 0,
    page_size: int = 1000,
    max_variants: int = 100000,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """Collect variants of a subset by paging its temporary collection (``searchMode`` 2),
    starting at page ``first_page`` (pages of ``page_size`` rows, as Gigwa numbers them).

    Whole pages are read until at least ``max_variants`` rows are collected, so the result
    can overshoot by less than one page -- that keeps ``next_page`` exact. Returns
    ``(variants, next_page, warning)``; ``next_page`` is ``None`` once the subset is
    exhausted.

    Pages are read with skip/limit over a sorted query, so they are only consistent if
    Gigwa's ordering stays stable between requests. Once, live, a walk returned some
    variants twice (and so missed others) -- not reproduced since. A walk that sees a
    variant twice is therefore re-read once; if it happens again the duplicates are
    dropped and ``warning`` says the list may be incomplete.
    """
    for attempt in range(2):
        variants, next_page, duplicates = _walk_subset(
            client, subset, first_page=first_page, page_size=page_size, max_variants=max_variants
        )
        if not duplicates:
            return variants, next_page, None
        notify(f"Gigwa returned {duplicates} variant(s) twice while paging; reading again…")
        time.sleep(2)
    return variants, next_page, (
        f"Gigwa returned {duplicates} variant(s) more than once while paging (unstable page "
        "order); duplicates were dropped, so the list may miss as many variants. Retry later, "
        "or use export_genotypes(subset=...) for an exact file."
    )


def _walk_subset(
    client: GigwaClient, subset: Subset, *, first_page: int, page_size: int, max_variants: int
) -> tuple[list[dict[str, Any]], int | None, int]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    page = int(first_page)
    retried = False
    while len(out) + duplicates < max_variants:
        variants, _next_token, count = run_with_subset(
            client, subset,
            lambda token: client.browse_selection(
                subset.selection, token=token, page=page, page_size=page_size
            ),
        )
        if not variants and page * page_size < subset.count and not retried:
            # The count says rows should be there but the temporary collection is empty:
            # the records were purged server-side without the token being rejected.
            retried = True
            refresh_subset(client, subset)
            continue
        for variant in variants:
            key = str(variant.get("id") or variant.get("variantDbId"))
            if key in seen:
                duplicates += 1
            else:
                seen.add(key)
                out.append(variant)
        # Gigwa numbers browse pages itself (pageToken = page index); the UI computes the
        # next one from the count rather than relying on a returned token.
        total = count or subset.count
        if not variants or (page + 1) * page_size >= total:
            return out, None, duplicates
        page += 1
    return out, page, duplicates
