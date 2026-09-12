"""`mappy` must stay excluded on Windows, where it has no wheels.

Both declarations are checked because they are consumed by different things: pip and uv
read ``pyproject.toml``, while ``requirements.txt`` is the pinned path. Each is parsed with
its own format's parser rather than scraped as text, so reformatting the file cannot make
these silently stop testing anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]

# (win32 excludes mappy; everything else installs it.)
PLATFORMS = [("win32", False), ("linux", True), ("darwin", True)]


def _find(requirements: list[str], name: str) -> Requirement:
    """Return the parsed requirement called *name*, or fail with what was actually found."""
    parsed = [Requirement(r) for r in requirements]
    matches = [r for r in parsed if r.name == name]
    assert matches, f"no {name!r} requirement found; got {sorted(r.name for r in parsed)}"
    assert len(matches) == 1, f"{name!r} declared {len(matches)} times"
    return matches[0]


def _installs_on(requirement: Requirement, platform: str) -> bool:
    """Whether *requirement* would be installed on *platform*. No marker means everywhere."""
    if requirement.marker is None:
        return True
    return requirement.marker.evaluate({"sys_platform": platform})


def _pyproject_dependencies() -> list[str]:
    """Project dependencies, parsed as TOML rather than scraped out of the raw text."""
    tomllib = pytest.importorskip(
        "tomllib", reason="tomllib is stdlib from 3.11; the 3.11-3.13 CI jobs cover this"
    )
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["dependencies"]


def _requirements_txt() -> list[str]:
    """Requirement lines, skipping blanks, comments and pip options."""
    lines = (ROOT / "requirements.txt").read_text().splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    ]


@pytest.mark.parametrize("platform,expected", PLATFORMS)
def test_pyproject_excludes_mappy_on_windows(platform, expected):
    mappy = _find(_pyproject_dependencies(), "mappy")
    assert mappy.marker is not None, f"mappy lost its platform marker: {mappy}"
    assert mappy.marker.evaluate({"sys_platform": platform}) is expected


@pytest.mark.parametrize("platform,expected", PLATFORMS)
def test_requirements_txt_excludes_mappy_on_windows(platform, expected):
    mappy = _find(_requirements_txt(), "mappy")
    assert mappy.marker is not None, f"mappy lost its platform marker: {mappy}"
    assert mappy.marker.evaluate({"sys_platform": platform}) is expected


def test_the_two_declarations_agree():
    """A marker fixed in one file and forgotten in the other is the likely regression."""
    pyproject = _find(_pyproject_dependencies(), "mappy")
    pinned = _find(_requirements_txt(), "mappy")
    for platform, _ in PLATFORMS:
        assert _installs_on(pyproject, platform) == _installs_on(pinned, platform), (
            f"pyproject.toml and requirements.txt disagree about installing mappy on "
            f"{platform}: {pyproject} vs {pinned}"
        )
