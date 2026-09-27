"""Growth ratchet for maintenance hotspots (maintenance rules 10–12).

A hotspot is a package module of at least ``FILE_THRESHOLD`` lines or a
function of at least ``FUNCTION_THRESHOLD`` lines. Every hotspot needs a
bounded exception in ``docs/operations/hotspots.json`` naming its rationale,
owner, next review and reopening event (rule 11). A hotspot that grows past its
recorded ``max_lines`` fails; raising the bound is itself the reviewed
exception. Shrinkage, resolved entries and overdue reviews are reported as debt
without failing the gate. Sizes select review; they are not a quality score.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

PACKAGE_PATH = Path("src") / "hermes_codex_router"
BASELINE_PATH = Path("docs") / "operations" / "hotspots.json"
FILE_THRESHOLD = 1500
FUNCTION_THRESHOLD = 200
_REQUIRED_TEXT = ("rationale", "owner", "reopen_event")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True, slots=True)
class HotspotAudit:
    errors: tuple[str, ...]
    debts: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _functions(node: ast.AST, prefix: str = "") -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}{child.name}"
            if not isinstance(child, ast.ClassDef) and child.end_lineno is not None:
                found.append((name, child.end_lineno - child.lineno + 1))
            found.extend(_functions(child, f"{name}."))
        else:
            found.extend(_functions(child, prefix))
    return found


def measure_hotspots(root: Path) -> dict[str, int]:
    """Return ``{target: lines}`` for every current file and function hotspot."""
    measured: dict[str, int] = {}
    for path in sorted((root / PACKAGE_PATH).glob("*.py")):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        lines = len(source.splitlines())
        if lines >= FILE_THRESHOLD:
            measured[relative] = lines
        for name, size in _functions(ast.parse(source, filename=relative)):
            if size >= FUNCTION_THRESHOLD:
                measured[f"{relative}::{name}"] = size
    return measured


def audit_hotspots(root: Path, *, today: date | None = None) -> HotspotAudit:
    root = root.resolve()
    current_date = today or date.today()
    document = json.loads((root / BASELINE_PATH).read_text(encoding="utf-8"))
    errors: list[str] = []
    debts: list[str] = []
    if document.get("schema_version") != 1:
        errors.append("hotspot baseline schema_version must be 1")
    entries: dict[str, dict[str, object]] = {}
    for entry in document.get("hotspots", []):
        target = entry.get("target")
        if not isinstance(target, str) or not target:
            errors.append("hotspot entry needs a target")
            continue
        if target in entries:
            errors.append(f"duplicate hotspot entry: {target}")
            continue
        entries[target] = entry
        max_lines = entry.get("max_lines")
        if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines < 1:
            errors.append(f"hotspot entry {target} needs a positive integer max_lines")
        for field in _REQUIRED_TEXT:
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"hotspot entry {target} needs a non-empty {field}")
        review = entry.get("next_review")
        if not isinstance(review, str) or _ISO_DATE.fullmatch(review) is None:
            errors.append(f"hotspot entry {target} needs next_review as YYYY-MM-DD")
        elif date.fromisoformat(review) < current_date:
            debts.append(f"bounded exception review overdue: {target} ({review})")

    measured = measure_hotspots(root)
    for target, lines in measured.items():
        entry = entries.get(target)
        if entry is None:
            errors.append(f"new hotspot needs a bounded exception: {target} ({lines} lines)")
            continue
        bound = entry.get("max_lines")
        if not isinstance(bound, int):
            continue
        if lines > bound:
            errors.append(
                f"hotspot grew without a bounded exception: {target} {lines} > {bound} lines"
            )
        elif lines < bound:
            debts.append(f"lower recorded bound: {target} is {lines} of {bound} lines")
    for target in entries:
        if target not in measured:
            debts.append(f"remove resolved hotspot entry: {target}")
    overdue = [debt for debt in debts if debt.startswith("bounded exception review overdue")]
    others = [debt for debt in debts if debt not in overdue]
    return HotspotAudit(tuple(errors), tuple(others + overdue))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maintenance hotspot growth ratchet")
    parser.add_argument("root", type=Path, nargs="?", default=Path("."))
    args = parser.parse_args(argv)
    result = audit_hotspots(args.root)
    print(json.dumps({"ok": result.ok, **asdict(result)}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
