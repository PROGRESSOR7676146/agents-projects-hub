from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.incoming_materials import (
    IncomingMaterialError,
    IncomingMaterialRecord,
    incoming_storage_root,
    prepare_incoming_materials,
)
from tests.test_claude_image_input import JPEG, PNG


class ClaudeMaterialInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state_path = self.root / "state.db"

    def record(
        self, content: bytes = PNG, mime: str = "image/png", position: int = 1
    ) -> IncomingMaterialRecord:
        raw = incoming_storage_root(self.state_path) / "aa" / f"material-{position}"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(content)
        return IncomingMaterialRecord(
            material_id=f"example-{position}",
            job_id="example-job",
            topic_id=1,
            project_id="example-project",
            execution_scope=str(self.project),
            agent_id="claude",
            session_id="example-session",
            session_generation=1,
            chat_id=-1001234567890,
            message_id=position,
            attachment_index=1,
            media_group_id="example-album",
            origin="productive",
            kind="document",
            content_kind="image",
            file_unique_id=None,
            display_name=f"image-{position}.png",
            mime_type=mime,
            declared_size=len(content),
            storage_path=str(raw),
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            status="stored",
            unavailable_code=None,
            unavailable_detail=None,
        )

    def prepare(self, records: tuple[IncomingMaterialRecord, ...], enabled: bool = True):
        return prepare_incoming_materials(
            records,
            state_path=self.state_path,
            execution_root=self.project,
            job_id="example-job",
            runtime="claude",
            claude_image_input=enabled,
        )

    def test_opt_in_preserves_exact_order_and_materializes_the_same_bytes(self) -> None:
        records = (self.record(), self.record(JPEG, "image/jpeg", 2))
        prepared = self.prepare(records)
        self.assertEqual([image.data for image in prepared.claude_images], [PNG, JPEG])
        self.assertEqual([image.position for image in prepared.claude_images], [1, 2])
        self.assertEqual(prepared.local_image_paths, ())
        self.assertEqual(prepared.notices, ())
        assert prepared.materialized_directory is not None
        self.assertEqual(
            [path.read_bytes() for path in sorted(prepared.materialized_directory.iterdir())],
            [PNG, JPEG],
        )
        self.assertNotIn(str(self.root), prepared.prompt_suffix)
        self.assertNotIn("localImage", prepared.prompt_suffix)
        for path in prepared.materialized_directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    def test_default_remains_explicitly_unavailable(self) -> None:
        prepared = self.prepare((self.record(),), False)
        self.assertEqual(prepared.claude_images, ())
        self.assertIn("no accepted native image-input", prepared.prompt_suffix)
        self.assertIn("no accepted native image-input", prepared.visible_notice)

    def test_supported_class_size_and_aggregate_exclusions_are_visible(self) -> None:
        large = PNG + b"x" * (2 * 1024 * 1024 - len(PNG))
        records = (
            self.record(b"GIF89afictional", "image/gif", 1),
            self.record(PNG + b"x" * (2 * 1024 * 1024), position=2),
            self.record(large, position=3),
            self.record(large, position=4),
            self.record(position=5),
        )
        prepared = self.prepare(records)
        self.assertEqual([image.position for image in prepared.claude_images], [3, 4])
        self.assertEqual(len(prepared.notices), 3)
        for name in ("image-1.png", "image-2.png", "image-5.png"):
            self.assertIn(name, prepared.prompt_suffix)
            self.assertIn(name, prepared.visible_notice)

    def test_no_path_reopen_for_snapshot_or_materialized_image(self) -> None:
        record = self.record()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("no path reopen")):
            prepared = self.prepare((record,))
        self.assertEqual(prepared.claude_images[0].data, PNG)

    def test_digest_size_signature_and_symlink_drift_refuse_before_input(self) -> None:
        record = self.record()
        for changed in (
            replace(record, sha256="0" * 64),
            replace(record, byte_size=1),
            replace(record, mime_type="image/jpeg"),
        ):
            with self.subTest(changed=changed.mime_type), self.assertRaises(IncomingMaterialError):
                self.prepare((changed,))
        assert record.storage_path is not None
        path = Path(record.storage_path)
        other = path.with_name("other")
        other.write_bytes(PNG)
        path.unlink()
        path.symlink_to(other)
        with self.assertRaises(IncomingMaterialError):
            self.prepare((record,))


if __name__ == "__main__":
    unittest.main()
