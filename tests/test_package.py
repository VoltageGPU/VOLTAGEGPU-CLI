"""Package-level guards: branding, Python support, version consistency."""

import ast
import re
import sys
from pathlib import Path

import pytest

import volt

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_FILES = sorted(
    [*ROOT.joinpath("volt").rglob("*.py"), ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "pyproject.toml",
     *ROOT.joinpath("examples").rglob("*.py")]
)
# The only file allowed to spell the internal names is the scrubber that removes them.
ALLOWED = {ROOT / "volt" / "sdk" / "errors.py"}
FORBIDDEN = re.compile(r"\b(?:lium|celium|bittensor|targon|datura)\b|decentrali[sz]ed|gpu cloud", re.IGNORECASE)


@pytest.mark.parametrize("path", [p for p in PUBLIC_FILES if p not in ALLOWED], ids=lambda p: str(p.relative_to(ROOT)))
def test_no_internal_supplier_name_in_public_files(path):
    text = path.read_text(encoding="utf-8")
    match = FORBIDDEN.search(text)
    assert match is None, f"{path.name} mentions {match.group(0)!r}"


def test_no_package_named_after_a_supplier():
    names = {p.name.lower() for p in ROOT.joinpath("volt").rglob("*")}
    assert not any("lium" in n or "targon" in n for n in names)


@pytest.mark.parametrize("path", sorted(ROOT.joinpath("volt").rglob("*.py")), ids=lambda p: p.name)
def test_sources_parse_as_python_3_10(path):
    ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))


def test_version_is_consistent():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{volt.__version__}"' in pyproject
    assert f"## [{volt.__version__}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_cli_strings_are_ascii():
    """Legacy Windows consoles print anything else as replacement characters."""
    for path in ROOT.joinpath("volt", "cli").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert text.isascii(), f"non-ASCII character in {path.name}"


def test_python_version_supported():
    assert sys.version_info >= (3, 10)
