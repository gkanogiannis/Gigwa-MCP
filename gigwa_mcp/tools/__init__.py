"""MCP tool definitions, grouped by area (connection, genotype, metadata)."""

from __future__ import annotations

import json
from typing import Any


def load_json_arg(value: Any) -> Any:
    """Decode a ``*_json`` tool argument.

    Some MCP clients decode a JSON-looking string argument into an object before sending
    it, so accept an already-decoded dict/list as well as a JSON string.
    """
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)
