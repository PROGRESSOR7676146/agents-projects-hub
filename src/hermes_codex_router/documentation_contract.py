from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote

_REQUIREMENT_DEFINITION = re.compile(r"\*\*(REQ-[A-Z0-9-]+) \(")
_MARKDOWN_LINK = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_NUMBERED_SECTION = re.compile(r"^## (\d+)\. ", re.MULTILINE)
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:")


@dataclass(frozen=True, slots=True)
class DocumentationAudit:
    requirement_count: int
    markdown_file_count: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _markdown_paths(root: Path) -> tuple[Path, ...]:
    paths = set(root.glob("*.md"))
    docs = root / "docs"
    if docs.is_dir():
        paths.update(docs.rglob("*.md"))
    return tuple(sorted(path.resolve() for path in paths if path.is_file()))


def _slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[\[`*_~]", "", value)
    value = re.sub(r"[^\w\- ]", "", value.casefold())
    return re.sub(r"\s+", "-", value.strip())


def _anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: Counter[str] = Counter()
    for heading in _HEADING.findall(text):
        base = _slug(heading)
        count = counts[base]
        counts[base] += 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    anchors.update(re.findall(r"<a\s+(?:name|id)=[\"']([^\"']+)[\"']", text))
    return anchors


def _link_destination(raw: str) -> str:
    destination = raw.strip()
    if destination.startswith("<") and ">" in destination:
        return destination[1 : destination.index(">")]
    return destination.split(maxsplit=1)[0]


def audit_documentation(root: Path) -> DocumentationAudit:
    root = root.resolve()
    product_root = root / "docs" / "product"
    manifest = json.loads((product_root / "requirements_manifest.json").read_text("utf-8"))
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("requirements manifest schema_version must be 1")
    expected = tuple(str(value) for value in manifest.get("requirement_ids", ()))
    if tuple(sorted(set(expected))) != expected:
        errors.append("requirements manifest IDs must be unique and sorted")

    documents = tuple(str(value) for value in manifest.get("documents", ()))
    definitions: list[str] = []
    sections: dict[str, str] = {}
    for relative in documents:
        path = (product_root / relative).resolve()
        try:
            path.relative_to(product_root)
        except ValueError:
            errors.append(f"requirements document escapes product directory: {relative}")
            continue
        if not path.is_file():
            errors.append(f"missing requirements document: {relative}")
            continue
        document_text = path.read_text("utf-8")
        definitions.extend(_REQUIREMENT_DEFINITION.findall(document_text))
        matches = tuple(_NUMBERED_SECTION.finditer(document_text))
        for index, match in enumerate(matches):
            section_id = match.group(1)
            if section_id in sections:
                errors.append(f"duplicate numbered requirements section: {section_id}")
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(document_text)
            sections[section_id] = document_text[match.start() : end].strip() + "\n"

    expected_sections = {
        str(key): str(value) for key, value in manifest.get("section_sha256", {}).items()
    }
    for section_id in sorted(expected_sections, key=int):
        section = sections.get(section_id)
        if section is None:
            errors.append(f"missing numbered requirements section: {section_id}")
        elif hashlib.sha256(section.encode()).hexdigest() != expected_sections[section_id]:
            errors.append(f"requirements section content changed: {section_id}")
    for section_id in sorted(set(sections) - set(expected_sections), key=int):
        if expected_sections:
            errors.append(f"unexpected numbered requirements section: {section_id}")

    counts = Counter(definitions)
    for requirement_id in sorted(identifier for identifier, count in counts.items() if count > 1):
        errors.append(f"duplicate requirement definition: {requirement_id}")
    for requirement_id in expected:
        if counts[requirement_id] == 0:
            errors.append(f"missing requirement definition: {requirement_id}")
    for requirement_id in sorted(set(definitions) - set(expected)):
        errors.append(f"unexpected requirement definition: {requirement_id}")

    markdown_paths = _markdown_paths(root)
    text_cache: dict[Path, str] = {}
    anchor_cache: dict[Path, set[str]] = {}
    for source in markdown_paths:
        text = source.read_text("utf-8")
        text_cache[source] = text
        for match in _MARKDOWN_LINK.finditer(text):
            destination = unquote(_link_destination(match.group(1)))
            if not destination or destination.startswith(_EXTERNAL_SCHEMES):
                continue
            target_text, separator, fragment = destination.partition("#")
            if target_text.startswith("/"):
                continue
            target = (source.parent / target_text).resolve() if target_text else source
            line = text.count("\n", 0, match.start()) + 1
            label = source.relative_to(root)
            try:
                target.relative_to(root)
            except ValueError:
                errors.append(f"{label}:{line}: link escapes repository: {destination}")
                continue
            if not target.exists():
                errors.append(f"{label}:{line}: missing link target: {target_text}")
                continue
            if separator and fragment and target.is_file() and target.suffix.casefold() == ".md":
                if target not in anchor_cache:
                    target_content = text_cache.get(target)
                    if target_content is None:
                        target_content = target.read_text("utf-8")
                    anchor_cache[target] = _anchors(target_content)
                if fragment not in anchor_cache[target]:
                    errors.append(f"{label}:{line}: missing anchor: {destination}")

    return DocumentationAudit(len(definitions), len(markdown_paths), tuple(errors))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="audit requirements inventory and Markdown links")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    result = audit_documentation(parser.parse_args(argv).root)
    print(json.dumps({"ok": result.ok, **asdict(result)}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
