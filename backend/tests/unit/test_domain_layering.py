"""ARCHITECTURE section 1, rule 1, enforced rather than reviewed.

    The domain layer must not import a concrete adapter implementation.
    Only the abstract types in base.py.

This is the G-C gate condition, and a gate checked once is a gate that holds
until the first person in a hurry. The stage that follows is D, where the API
layer wires providers in for real and ``from app.services.scoring.fake import
FakeScorer`` becomes a one-line shortcut that works.

Checked against the import statements, which is the one method that can tell a
permitted import from a forbidden one. Inspecting ``sys.modules`` after a real
import looks stronger and is not: importing ``app.services.scoring.base``
necessarily executes ``app/services/scoring/__init__.py``, and that module
exports the registry by design (CLAUDE.md section 4). So the permitted import
and the forbidden one leave identical traces in ``sys.modules``, and a check
built on it either rejects ``base`` or accepts ``fake``.

Every file under the domain packages is parsed, so an adapter reached through
another domain module is caught at the module that actually names it.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

DOMAIN_PACKAGES = ("mastery", "media", "entitlements", "experiments")
ADAPTER_PACKAGES = ("scoring", "tts", "payments", "llm")

#: The one module name inside an adapter package the domain layer may name.
PERMITTED_SUBMODULE = "base"


def domain_files() -> list[Path]:
    found: list[Path] = []
    for package in DOMAIN_PACKAGES:
        found.extend(sorted((APP_ROOT / "services" / package).rglob("*.py")))
    return found


def module_name_of(path: Path) -> str:
    return str(path.relative_to(APP_ROOT.parent).with_suffix("")).replace("/", ".")


def resolve(node: ast.ImportFrom, containing_module: str) -> str:
    """The absolute module an ``ImportFrom`` refers to.

    Relative imports are resolved rather than skipped: ``from ..scoring.fake
    import FakeScorer`` is the same violation written differently, and a check
    that only understood absolute paths would wave it through.
    """
    if not node.level:
        return node.module or ""
    package = containing_module.rsplit(".", node.level)[0]
    return f"{package}.{node.module}" if node.module else package


def violations_in(path: Path) -> list[str]:
    """Forbidden adapter imports in one file, as readable messages."""
    containing = module_name_of(path)
    found: list[str] = []

    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        targets: list[tuple[int, str]] = []
        if isinstance(node, ast.Import):
            targets = [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = resolve(node, containing)
            # ``from app.services.scoring import base`` is permitted while
            # ``from app.services.scoring import build_scorer`` is not, and both
            # carry the same module — so the imported names decide.
            if module in {f"app.services.{a}" for a in ADAPTER_PACKAGES}:
                targets = [(node.lineno, f"{module}.{alias.name}") for alias in node.names]
            else:
                targets = [(node.lineno, module)]

        for lineno, target in targets:
            for adapter in ADAPTER_PACKAGES:
                prefix = f"app.services.{adapter}"
                if target == prefix or (
                    target.startswith(f"{prefix}.") and target.split(".")[3] != PERMITTED_SUBMODULE
                ):
                    found.append(f"{path.relative_to(APP_ROOT.parent)}:{lineno} imports {target}")

    return found


def test_the_domain_layer_imports_no_adapter_implementation() -> None:
    files = domain_files()
    assert files, "no domain modules found; the package layout moved"

    found = [v for path in files for v in violations_in(path)]

    assert not found, (
        "the domain layer reached into an adapter implementation:\n  "
        + "\n  ".join(found)
        + "\n\nOnly base.py is importable (ARCHITECTURE section 1). Depend on "
        "the abstract type and let the caller pass an implementation in."
    )


def test_every_package_under_services_is_classified() -> None:
    """A package added or renamed without being listed would pass silently."""
    on_disk = {
        p.name
        for p in (APP_ROOT / "services").iterdir()
        # A directory with no Python in it is not a package, whatever it is.
        if p.is_dir() and not p.name.startswith("_") and any(p.glob("*.py"))
    }

    assert on_disk == set(DOMAIN_PACKAGES) | set(ADAPTER_PACKAGES), (
        f"app/services holds {sorted(on_disk)}; this test knows about "
        f"{sorted(set(DOMAIN_PACKAGES) | set(ADAPTER_PACKAGES))}. Classify the "
        "new package as domain or adapter before the gate can mean anything."
    )
