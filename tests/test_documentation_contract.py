from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.documentation_contract import audit_documentation


class DocumentationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "docs" / "product").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_manifest(
        self,
        *,
        documents: tuple[str, ...],
        requirement_ids: tuple[str, ...],
        section_sha256: dict[str, str] | None = None,
    ) -> None:
        (self.root / "docs" / "product" / "requirements_manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "documents": documents,
                    "section_sha256": section_sha256 or {},
                    "requirement_ids": requirement_ids,
                }
            ),
            encoding="utf-8",
        )

    def test_inventory_and_relative_links_pass_across_modules(self) -> None:
        self.write_manifest(
            documents=("PRODUCT_REQUIREMENTS.md", "identity.md"),
            requirement_ids=("REQ-ID-001", "REQ-ID-002"),
        )
        (self.root / "README.md").write_text(
            "[Requirements](docs/product/PRODUCT_REQUIREMENTS.md#modules)\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "product" / "PRODUCT_REQUIREMENTS.md").write_text(
            "# Requirements\n\n## Modules\n\n[Identity](identity.md#identity)\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "product" / "identity.md").write_text(
            "# Identity\n\n- **REQ-ID-001 (Accepted):** One.\n- **REQ-ID-002 (Accepted):** Two.\n",
            encoding="utf-8",
        )

        result = audit_documentation(self.root)

        self.assertEqual(result.errors, ())
        self.assertEqual(result.requirement_count, 2)
        self.assertEqual(result.markdown_file_count, 3)

    def test_missing_and_duplicate_requirement_definitions_fail(self) -> None:
        self.write_manifest(
            documents=("PRODUCT_REQUIREMENTS.md",),
            requirement_ids=("REQ-ID-001", "REQ-ID-002"),
        )
        (self.root / "docs" / "product" / "PRODUCT_REQUIREMENTS.md").write_text(
            "# Requirements\n\n"
            "- **REQ-ID-001 (Accepted):** One.\n"
            "- **REQ-ID-001 (Accepted):** Duplicate.\n",
            encoding="utf-8",
        )

        result = audit_documentation(self.root)

        self.assertEqual(
            result.errors,
            (
                "duplicate requirement definition: REQ-ID-001",
                "missing requirement definition: REQ-ID-002",
            ),
        )

    def test_missing_file_and_anchor_links_fail(self) -> None:
        self.write_manifest(documents=("PRODUCT_REQUIREMENTS.md",), requirement_ids=())
        (self.root / "docs" / "product" / "PRODUCT_REQUIREMENTS.md").write_text(
            "# Requirements\n\n[Missing file](missing.md)\n"
            "[Missing anchor](PRODUCT_REQUIREMENTS.md#absent)\n",
            encoding="utf-8",
        )

        result = audit_documentation(self.root)

        self.assertEqual(
            result.errors,
            (
                "docs/product/PRODUCT_REQUIREMENTS.md:3: missing link target: missing.md",
                "docs/product/PRODUCT_REQUIREMENTS.md:4: missing anchor: "
                "PRODUCT_REQUIREMENTS.md#absent",
            ),
        )

    def test_numbered_section_content_hash_prevents_loss_during_split(self) -> None:
        section = "## 1. Mission\n\nOriginal normative text.\n"
        digest = hashlib.sha256(section.encode()).hexdigest()
        self.write_manifest(
            documents=("PRODUCT_REQUIREMENTS.md",),
            requirement_ids=(),
            section_sha256={"1": digest},
        )
        (self.root / "docs" / "product" / "PRODUCT_REQUIREMENTS.md").write_text(
            "# Requirements\n\n## 1. Mission\n\nChanged normative text.\n",
            encoding="utf-8",
        )

        result = audit_documentation(self.root)

        self.assertEqual(result.errors, ("requirements section content changed: 1",))

    def test_external_and_absolute_links_are_outside_repository_contract(self) -> None:
        self.write_manifest(documents=("PRODUCT_REQUIREMENTS.md",), requirement_ids=())
        (self.root / "docs" / "product" / "PRODUCT_REQUIREMENTS.md").write_text(
            "# Requirements\n\n[Web](https://example.com/path)\n[Mail](mailto:team@example.com)\n",
            encoding="utf-8",
        )

        result = audit_documentation(self.root)

        self.assertEqual(result.errors, ())


if __name__ == "__main__":
    unittest.main()
