import re
from pathlib import Path

RELEASE = (Path(__file__).parents[1] / ".github" / "workflows" / "release-pypi.yml").read_text()
PYPI_BUILD_SHA = "8f422c652d836c40f9cc5a9d893d4120b26bc681"
PYPI_PUBLISH_SHA = "ba38be9e461d3875417946c167d0b5f3d385a247"


def test_release_actions_are_immutable() -> None:
    assert "yasyf/homebrew-tap/.github/workflows/release-pypi-build.yml@" + PYPI_BUILD_SHA in RELEASE
    assert "pypa/gh-action-pypi-publish@" + PYPI_PUBLISH_SHA in RELEASE
    assert not re.search(
        r"(?:release-pypi-build\.yml|gh-action-pypi-publish)@(?![0-9a-f]{40}(?:\s|$))[^\s]+",
        RELEASE,
    )


def test_pypi_build_contract_is_preserved() -> None:
    assert "      dist-name: dly" in RELEASE
    assert '      python-version: "3.14"' in RELEASE
