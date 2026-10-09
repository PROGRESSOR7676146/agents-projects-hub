"""Static regression for the retired Hub roles; not an execution sandbox."""

from __future__ import annotations

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path

_PACKAGE = "hermes_codex_router"
_RETIRED = frozenset(
    f"{_PACKAGE}.{name}"
    for name in (
        "review_materials",
        "review_bridge_protocol",
        "review_bridge_attempt",
        "review_bridge_sequence",
        "review_bridge_write_buffer",
    )
)


def _forbidden_edges(package: Path) -> list[tuple[str, int, str]]:
    forbidden: list[tuple[str, int, str]] = []
    paths = sorted(package.rglob("*.py"))
    if not paths:
        raise ValueError("Production package source is missing")
    for path in paths:
        relative = path.relative_to(package)
        parts = [_PACKAGE, *relative.with_suffix("").parts]
        is_package = parts[-1] == "__init__"
        if is_package:
            parts.pop()
        caller = ".".join(parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative.as_posix())
        if caller in _RETIRED:
            continue
        caller_package = caller if is_package else caller.rpartition(".")[0]
        for node in ast.walk(tree):
            targets: set[str] = set()
            if isinstance(node, ast.Import):
                targets.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = importlib.util.resolve_name("." * node.level + base, caller_package)
                targets.add(base)
                targets.update(f"{base}.{alias.name}" for alias in node.names)
            else:
                continue
            for retired in sorted(_RETIRED):
                if any(target == retired or target.startswith(retired + ".") for target in targets):
                    forbidden.append((relative.as_posix(), node.lineno, retired))
    return sorted(forbidden)


class RetiredHubRolesTests(unittest.TestCase):
    def _scan(self, sources: dict[str, str]) -> list[tuple[str, int, str]]:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / _PACKAGE
            package.mkdir()
            for name, source in sources.items():
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
            return _forbidden_edges(package)

    def test_retired_primitives_have_no_static_production_callers(self) -> None:
        package = Path(__file__).resolve().parents[1] / "src" / _PACKAGE
        self.assertEqual(
            _forbidden_edges(package),
            [],
            "ADR 0065 retired Hub roles; new production wiring requires a new owner decision.",
        )

    def test_detects_each_retired_module_in_supported_import_forms(self) -> None:
        for retired in sorted(_RETIRED):
            short = retired.rsplit(".", 1)[-1]
            for source in (
                f"import {retired}",
                f"import {retired} as dormant",
                f"import {retired}.child",
                f"from {retired} import symbol",
                f"from {retired} import *",
                f"from {_PACKAGE} import {short}",
                f"from {_PACKAGE} import {short} as dormant",
                f"from .{short} import symbol",
                f"from . import {short}",
                f"def later():\n    import {retired}",
                f"if False:\n    from . import {short}",
            ):
                with self.subTest(retired=retired, source=source):
                    found = self._scan({"worker.py": source})
                    self.assertEqual(len(found), 1)
                    self.assertEqual(found[0][0], "worker.py")
                    self.assertEqual(found[0][2], retired)

    def test_detects_relative_imports_in_nested_modules_and_package_initializers(self) -> None:
        for caller, source in (
            ("__init__.py", "from . import review_materials"),
            ("nested/__init__.py", "from .. import review_materials"),
            ("nested/worker.py", "from .. import review_materials"),
            ("nested/deeper/worker.py", "from ...review_materials import ReviewCapsule"),
        ):
            with self.subTest(caller=caller):
                self.assertEqual(
                    self._scan({caller: source}),
                    [(caller, 1, f"{_PACKAGE}.review_materials")],
                )

    def test_reports_all_edges_and_locations_without_hiding_nested_callers(self) -> None:
        self.assertEqual(
            self._scan(
                {
                    "worker.py": (
                        "import os\n"
                        "def later():\n"
                        "    from . import review_materials, review_bridge_protocol\n"
                        "from .review_bridge_attempt import BridgeAttemptGate\n"
                    ),
                    "nested/worker.py": "from .. import review_bridge_sequence\n",
                }
            ),
            [
                ("nested/worker.py", 1, f"{_PACKAGE}.review_bridge_sequence"),
                ("worker.py", 3, f"{_PACKAGE}.review_bridge_protocol"),
                ("worker.py", 3, f"{_PACKAGE}.review_materials"),
                ("worker.py", 4, f"{_PACKAGE}.review_bridge_attempt"),
            ],
        )

    def test_retained_internal_edges_and_shared_provider_code_remain_permitted(self) -> None:
        self.assertEqual(
            self._scan(
                {
                    "review_bridge_attempt.py": (
                        "from .review_materials import ReviewCapsule\n"
                        "from .review_bridge_protocol import BridgeFrame\n"
                    ),
                    "review_materials.py": "from .process_namespace import NamespaceError\n",
                    "claude_file_sandbox.py": "from .process_namespace import NamespaceRuntime\n",
                    "worker.py": (
                        "from . import metadata\n"
                        "import third_party.review_materials\n"
                        "from .review_materials_v2 import Symbol\n"
                    ),
                    "nested/review_materials.py": "from .. import process_namespace\n",
                }
            ),
            [],
        )

    def test_same_named_nested_module_is_not_exempt_from_the_guard(self) -> None:
        self.assertEqual(
            self._scan({"nested/review_materials.py": "from .. import review_materials"}),
            [("nested/review_materials.py", 1, f"{_PACKAGE}.review_materials")],
        )

    def test_invalid_source_cannot_silently_pass(self) -> None:
        with self.assertRaises(SyntaxError):
            self._scan({"worker.py": "from . import ("})

    def test_missing_source_cannot_silently_pass(self) -> None:
        with self.assertRaises(ValueError):
            self._scan({})


if __name__ == "__main__":
    unittest.main()
