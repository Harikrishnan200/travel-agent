"""The pinned dependencies must be the ones we actually test against.

This exists because they drifted. groq and google-genai were upgraded locally
while developing and requirements.txt was left behind, so Docker built an image
with SDKs two major versions older than anything that had been run. Everything
passed locally; Gemini failed in the container with schema validation errors.

A version pin that nobody checks is a comment.
"""

import re
from importlib import metadata
from pathlib import Path

import pytest

REQUIREMENTS = Path(__file__).parent.parent / "requirements.txt"
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==(.+)$")


def pinned_versions() -> dict[str, str]:
    pins = {}
    for line in REQUIREMENTS.read_text().splitlines():
        match = PIN.match(line.strip())
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def test_requirements_pins_something():
    assert len(pinned_versions()) > 5


@pytest.mark.parametrize("package", sorted(pinned_versions()))
def test_the_installed_version_is_the_pinned_one(package):
    """If this fails, Docker installs something nobody has run."""
    pinned = pinned_versions()[package]
    try:
        installed = metadata.version(package)
    except metadata.PackageNotFoundError:  # pragma: no cover
        pytest.skip(f"{package} is not installed in this environment")

    assert installed == pinned, (
        f"requirements.txt pins {package}=={pinned} but {installed} is installed. "
        f"Update the pin, or install the pinned version - Docker builds from this file."
    )
