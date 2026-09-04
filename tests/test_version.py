"""Release version has one authoritative source."""

from importlib.metadata import version

import gigwa_mcp
from gigwa_mcp.server import mcp


def test_distribution_module_and_server_versions_agree():
    expected = version("gigwa-mcp")
    assert expected == "1.9.1"
    assert gigwa_mcp.__version__ == expected
    assert mcp._lowlevel_server.version == expected
