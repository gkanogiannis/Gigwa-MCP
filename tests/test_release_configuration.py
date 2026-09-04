"""Static release/container invariants that do not need Docker or registry access."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_container_defaults_to_stdio_and_logs_to_stderr():
    dockerfile = (ROOT / "Dockerfile").read_text()
    entrypoint = (ROOT / "entrypoint.sh").read_text()
    assert "ENV GIGWA_MCP_PORT=" not in dockerfile
    assert 'STDIO mode" >&2' in entrypoint
    assert 'HTTP mode on port $GIGWA_MCP_PORT" >&2' in entrypoint


def test_publish_workflow_tags_version_and_latest_together():
    workflow = (ROOT / ".github/workflows/publish-docker.yml").read_text()
    assert "${{ env.IMAGE }}:${{ inputs.tag }}" in workflow
    assert "${{ env.IMAGE }}:latest" in workflow
    assert "latest_digest" in workflow and "version_digest" in workflow
