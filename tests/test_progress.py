"""Progress reporting: notify() safety, the progress_tool decorator, and end-to-end
delivery of notifications/progress from a tool body through FastMCP."""

from __future__ import annotations

import asyncio

from gigwa_mcp import progress
from gigwa_mcp.server import mcp, progress_tool


def test_notify_is_noop_without_reporter():
    # No active reporter (default): must not raise and must do nothing.
    progress.notify("hello", 1, 2)


def test_notify_noop_after_reset():
    token = progress.set_reporter(lambda *a: None)
    progress.reset_reporter(token)
    progress.notify("hello")  # reporter cleared -> no-op, no error


def test_progress_tool_preserves_schema_and_hides_ctx():
    @progress_tool()
    def sample_tool(variant_set_db_id: str, max_markers: int = 10) -> str:
        """Sample."""
        return f"{variant_set_db_id}:{max_markers}"

    tool = mcp._tool_manager.get_tool("sample_tool")
    props = tool.parameters["properties"]
    assert set(props) == {"variant_set_db_id", "max_markers"}  # ctx excluded
    assert "ctx" not in props
    assert tool.parameters["required"] == ["variant_set_db_id"]
    mcp._tool_manager.remove_tool("sample_tool")


def test_progress_tool_body_runs_and_can_notify_end_to_end():
    emitted: list[tuple] = []

    @progress_tool()
    def worker(x: int) -> str:
        """Emit a couple of progress pings from the (threaded, sync) body."""
        progress.notify("step 1", 1, 2)
        progress.notify("step 2", 2, 2)
        return f"done {x}"

    # A fake Context whose report_progress records what the body emitted.
    class FakeCtx:
        async def report_progress(self, prog, total, message):
            emitted.append((prog, total, message))

    async def run():
        return await mcp._tool_manager.get_tool("worker").fn(x=5, ctx=FakeCtx())

    result = asyncio.run(run())
    text = result[0].text if isinstance(result, (list, tuple)) else str(result)
    assert "done 5" in text
    assert emitted == [(1.0, 2, "step 1"), (2.0, 2, "step 2")]
    mcp._tool_manager.remove_tool("worker")
