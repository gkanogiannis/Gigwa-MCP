"""Real-process MCP handshakes for both advertised transports."""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

import httpx


INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "pytest", "version": "1"}},
}


def _stdout_queue(process: subprocess.Popen) -> "queue.Queue[str | None]":
    """Lines from the child's stdout, pumped by a background thread.

    A thread rather than ``select``: on Windows ``select`` accepts only sockets, so polling
    a subprocess pipe with it raises ``OSError: [WinError 10038]``. The reader is created
    once per process and cached on it, because a second one would steal lines from the
    first. ``None`` is queued at EOF.
    """
    existing = getattr(process, "_stdout_lines", None)
    if existing is not None:
        return existing
    lines: "queue.Queue[str | None]" = queue.Queue()

    def pump() -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    process._stdout_lines = lines
    return lines


def _read_response(process: subprocess.Popen, request_id: int, timeout: float = 10) -> dict:
    lines = _stdout_queue(process)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:  # child closed stdout; it has died or finished
            break
        message = json.loads(line)
        if message.get("id") == request_id:
            return message
    # Surface why, rather than only that it timed out: a child that failed to start says so
    # on stderr, and that is far more useful than a bare timeout on a platform we cannot
    # reproduce locally.
    if process.poll() is not None:
        stderr = process.stderr.read() if process.stderr else ""
        raise AssertionError(
            f"server exited with code {process.returncode} before answering request "
            f"{request_id}; stderr:\n{stderr}"
        )
    raise AssertionError(f"timed out waiting for JSON-RPC response {request_id}")


def test_stdio_initialize_and_tools_list():
    process = subprocess.Popen(
        [sys.executable, "-m", "gigwa_mcp", "--stdio"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    try:
        process.stdin.write(json.dumps(INITIALIZE) + "\n")
        process.stdin.flush()
        initialized = _read_response(process, 1)
        assert initialized["result"]["serverInfo"]["name"] == "gigwa"
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        process.stdin.flush()
        tools = _read_response(process, 2)["result"]["tools"]
        assert len(tools) == 36
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_http_initialize_notification_and_tools_list():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "gigwa_mcp", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "GIGWA_MCP_HOST": "127.0.0.1"},
    )
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"accept": "application/json", "content-type": "application/json"}
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                response = httpx.post(url, json=INITIALIZE, headers=headers, timeout=1)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("HTTP MCP server did not become ready")
            time.sleep(0.1)
        session = response.headers["mcp-session-id"]
        session_headers = {**headers, "mcp-session-id": session}
        notification = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": 99, "method": "notifications/initialized", "params": None},
            headers=session_headers,
        )
        assert notification.status_code == 202
        listed = httpx.post(
            url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=session_headers,
        )
        assert listed.status_code == 200
        assert len(listed.json()["result"]["tools"]) == 36
    finally:
        process.terminate()
        process.wait(timeout=5)
