import re
from pathlib import Path

RELEASE = (Path(__file__).parents[1] / ".github" / "workflows" / "release-pypi.yml").read_text()
PYPI_BUILD_SHA = "7cc8a6c981cbec10fcb7f19bd75b36e9ee65ea7e"
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
