"""Progress reporting for long-running tools.

FastMCP emits ``notifications/progress`` from ``Context.report_progress`` — but that is an
async call, while our tool bodies (and the client/analysis helpers they call) are
synchronous and, under :func:`gigwa_mcp.server.progress_tool`, run in an AnyIO worker
thread. This module bridges the two: a tool installs the active reporter (the bound
``ctx.report_progress``) into a context variable, and any deep helper can then call the
module-level :func:`notify` without threading a ``Context`` through its signature.

``notify`` is a safe no-op when no reporter is installed (plain ``pytest`` runs, or a
client that did not send a ``progressToken``), so helpers can call it unconditionally.
"""

from __future__ import annotations

import contextvars
from typing import Any, Awaitable, Callable, Protocol

from anyio import from_thread


class _Reporter(Protocol):
    def __call__(self, progress: float, total: float | None, message: str | None) -> Awaitable[Any]: ...


# The active reporter is ``ctx.report_progress`` (async). None => progress disabled.
_reporter: contextvars.ContextVar["_Reporter | None"] = contextvars.ContextVar(
    "gigwa_progress_reporter", default=None
)


def set_reporter(reporter: "_Reporter | None") -> contextvars.Token:
    """Install *reporter* as the active progress sink; returns a token for :func:`reset_reporter`."""
    return _reporter.set(reporter)


def reset_reporter(token: contextvars.Token) -> None:
    """Restore the previously-active reporter."""
    _reporter.reset(token)


def notify(message: str, progress: float | None = None, total: float | None = None) -> None:
    """Emit a progress update, if a reporter is active. Safe no-op otherwise.

    ``progress``/``total`` drive a determinate progress bar; omit them for an indeterminate
    "still working" ping (the *message* still shows). Never raises — progress reporting must
    not break the tool it is annotating, so failures (no worker-thread portal, no
    ``progressToken``, transport error) are swallowed.
    """
    reporter = _reporter.get()
    if reporter is None:
        return
    try:
        # We run inside an AnyIO worker thread (see progress_tool); hop back to the event
        # loop to await the async report_progress there.
        from_thread.run(reporter, float(progress or 0.0), total, message)
    except Exception:  # noqa: BLE001 - progress is best-effort
        pass
