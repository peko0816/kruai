"""The provider base.py files must stay byte-identical to interfaces/.

CLAUDE.md section 4 makes those four files normative: copied as-is, signatures
unchanged. That is what lets M0 swap a vendor in by adding one class instead of
touching every caller — the guarantee is worthless the moment someone
"improves" a signature locally.

pyproject.toml excludes these paths from ruff precisely because they are not
our code to style. This test is the other half of that trade: the exemption
buys no place to hide, because any edit at all fails here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INTERFACES = _REPO_ROOT / "interfaces"
_SERVICES = _REPO_ROOT / "backend" / "app" / "services"

#: Source of truth -> where CLAUDE.md section 4 says it must be copied.
_NORMATIVE_COPIES: dict[str, str] = {
    "scoring_base.py": "scoring/base.py",
    "tts_base.py": "tts/base.py",
    "payments_base.py": "payments/base.py",
    "llm_base.py": "llm/base.py",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(("source", "target"), sorted(_NORMATIVE_COPIES.items()))
def test_copy_is_byte_identical(source: str, target: str) -> None:
    """Skips only for interfaces not yet copied; never for one that drifted."""
    origin = _INTERFACES / source
    copy = _SERVICES / target

    assert origin.exists(), f"normative interface missing: interfaces/{source}"
    if not copy.exists():
        pytest.skip(f"services/{target} lands in its own BACKLOG entry")

    assert digest(copy) == digest(origin), (
        f"services/{target} has diverged from interfaces/{source}. "
        "These files are normative (CLAUDE.md section 4) — restore the copy "
        "rather than editing it, and if the interface itself is wrong, change "
        "interfaces/ first and re-copy."
    )


def test_every_interface_has_a_declared_destination() -> None:
    """A new interfaces/*.py with nowhere to go would silently skip every check."""
    on_disk = {path.name for path in _INTERFACES.glob("*_base.py")}
    assert on_disk == set(_NORMATIVE_COPIES), (
        "interfaces/ and the destination table disagree; update "
        "_NORMATIVE_COPIES and CLAUDE.md section 4 together"
    )
