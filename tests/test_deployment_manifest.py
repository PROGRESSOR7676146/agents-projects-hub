from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from hermes_codex_router.deployment_manifest import (
    DeploymentManifestError,
    create_deployment_manifest,
    verify_deployment_manifest,
)
from hermes_codex_router.migrations import migrate_database


def _wheel(
    path: Path,
    *,
    version: str,
    git_sha: str,
    schema_max: int = 21,
    clean: bool = True,
    explicit_artifact_loss_declaration: bool | None = True,
) -> Path:
    build_info = "\n".join(
        (
            f"PACKAGE_VERSION = {version!r}",
            f"GIT_SHA = {git_sha!r}",
            "BUILT_AT = '2026-09-05T12:00:00+00:00'",
            f"CLEAN_TREE = {clean!r}",
            "",
        )
    )
    compatibility = "\n".join(
        (
            "MIN_SUPPORTED_SCHEMA_VERSION = 1",
            f"MAX_SUPPORTED_SCHEMA_VERSION = {schema_max}",
            "TARGET_SCHEMA_VERSION = MAX_SUPPORTED_SCHEMA_VERSION",
            "",
        )
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hermes_codex_router/_build_info.py", build_info)
        archive.writestr("hermes_codex_router/schema_compatibility.py", compatibility)
        if schema_max >= 59 and type(explicit_artifact_loss_declaration) is bool:
            archive.writestr(
                "hermes_codex_router/artifact_capabilities.py",
                "EXPLICIT_ARTIFACT_LOSS_DECLARATION = "
                + repr(explicit_artifact_loss_declaration)
                + "\n",
            )
        archive.writestr(
            f"agents_projects_hub-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: agents-projects-hub\nVersion: {version}\n",
        )
    return path


class DeploymentManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.active = _wheel(
            self.base / "agents_projects_hub-0.7.0-py3-none-any.whl",
            version="0.7.0",
            git_sha="a" * 40,
        )
        self.rollback = _wheel(
            self.base / "agents_projects_hub-0.6.0-py3-none-any.whl",
            version="0.6.0",
            git_sha="b" * 40,
        )
        self.config = self.base / "hub.json"
        self.config.write_text('{"schema_version": 1}\n', encoding="utf-8")
        os.chmod(self.config, 0o600)
        self.backup = self.base / "state-v20.db"
        migrate_database(self.backup, create_backup=False)
        connection = sqlite3.connect(self.backup)
        connection.execute("PRAGMA user_version = 20")
        connection.commit()
        connection.close()
        os.chmod(self.backup, 0o600)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_manifest_binds_both_artifacts_config_backup_and_schema_gate(self) -> None:
        manifest_path = self.base / "deployment.json"
        created = create_deployment_manifest(
            manifest_path,
            active_artifact=self.active,
            rollback_artifact=self.rollback,
            configuration=self.config,
            state_backup=self.backup,
            created_at=datetime(2026, 9, 5, 13, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(created.target_schema_version, 21)
        self.assertEqual(created.state_backup.schema_version, 20)
        self.assertEqual(created.active_artifact.package_version, "0.7.0")
        self.assertEqual(created.rollback_artifact.package_version, "0.6.0")
        self.assertEqual(verify_deployment_manifest(manifest_path), created)

        active_state = self.base / "active-state.db"
        shutil.copy2(self.backup, active_state)
        connection = sqlite3.connect(active_state)
        connection.execute("PRAGMA user_version = 21")
        connection.commit()
        connection.close()
        os.chmod(active_state, 0o600)
        self.assertEqual(
            verify_deployment_manifest(manifest_path, state_path=active_state),
            created,
        )

    def test_incompatible_rollback_artifact_is_rejected(self) -> None:
        incompatible = _wheel(
            self.base / "agents_projects_hub-0.5.0-py3-none-any.whl",
            version="0.5.0",
            git_sha="c" * 40,
            schema_max=20,
        )

        with self.assertRaisesRegex(
            DeploymentManifestError,
            "rollback artifact cannot open the target schema",
        ):
            create_deployment_manifest(
                self.base / "deployment.json",
                active_artifact=self.active,
                rollback_artifact=incompatible,
                configuration=self.config,
                state_backup=self.backup,
            )

    def test_dirty_or_same_release_artifacts_are_rejected(self) -> None:
        dirty = _wheel(
            self.base / "dirty.whl",
            version="0.7.0",
            git_sha="d" * 40,
            clean=False,
        )
        with self.assertRaisesRegex(DeploymentManifestError, "not a clean-tree"):
            create_deployment_manifest(
                self.base / "dirty.json",
                active_artifact=dirty,
                rollback_artifact=self.rollback,
                configuration=self.config,
                state_backup=self.backup,
            )
        with self.assertRaisesRegex(DeploymentManifestError, "distinct releases"):
            create_deployment_manifest(
                self.base / "same.json",
                active_artifact=self.active,
                rollback_artifact=self.active,
                configuration=self.config,
                state_backup=self.backup,
            )

    def test_verification_detects_artifact_config_and_manifest_tampering(self) -> None:
        manifest_path = self.base / "deployment.json"
        create_deployment_manifest(
            manifest_path,
            active_artifact=self.active,
            rollback_artifact=self.rollback,
            configuration=self.config,
            state_backup=self.backup,
        )

        self.config.write_text('{"schema_version": 2}\n', encoding="utf-8")
        with self.assertRaisesRegex(DeploymentManifestError, "configuration does not match"):
            verify_deployment_manifest(manifest_path)

        self.config.write_text('{"schema_version": 1}\n', encoding="utf-8")
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["target_schema_version"] = 20
        manifest_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        with self.assertRaisesRegex(DeploymentManifestError, "target schema"):
            verify_deployment_manifest(manifest_path)

    def test_manifest_and_private_inputs_must_not_be_symlinks_or_group_readable(self) -> None:
        os.chmod(self.config, 0o644)
        with self.assertRaisesRegex(DeploymentManifestError, "mode 0600"):
            create_deployment_manifest(
                self.base / "permissions.json",
                active_artifact=self.active,
                rollback_artifact=self.rollback,
                configuration=self.config,
                state_backup=self.backup,
            )

    def test_schema59_manifest_requires_true_active_and_false_rollback_capabilities(self) -> None:
        active = _wheel(
            self.base / "active-schema59.whl",
            version="0.7.1",
            git_sha="c" * 40,
            schema_max=59,
            explicit_artifact_loss_declaration=True,
        )
        unsafe_rollback = _wheel(
            self.base / "unsafe-rollback-schema59.whl",
            version="0.7.2",
            git_sha="d" * 40,
            schema_max=59,
            explicit_artifact_loss_declaration=True,
        )
        with self.assertRaisesRegex(
            DeploymentManifestError,
            "rollback schema-59 artifact must refuse artifact-loss declarations",
        ):
            create_deployment_manifest(
                self.base / "unsafe-schema59.json",
                active_artifact=active,
                rollback_artifact=unsafe_rollback,
                configuration=self.config,
                state_backup=self.backup,
            )

        safe_rollback = _wheel(
            self.base / "safe-rollback-schema59.whl",
            version="0.7.3",
            git_sha="e" * 40,
            schema_max=59,
            explicit_artifact_loss_declaration=False,
        )
        created = create_deployment_manifest(
            self.base / "safe-schema59.json",
            active_artifact=active,
            rollback_artifact=safe_rollback,
            configuration=self.config,
            state_backup=self.backup,
        )
        self.assertTrue(created.active_artifact.explicit_artifact_loss_declaration)
        self.assertFalse(created.rollback_artifact.explicit_artifact_loss_declaration)

    def test_schema59_wheel_cannot_omit_its_declaration_capability(self) -> None:
        wheel = _wheel(
            self.base / "missing-capability-schema59.whl",
            version="0.7.1",
            git_sha="f" * 40,
            schema_max=59,
            explicit_artifact_loss_declaration=None,
        )
        with self.assertRaisesRegex(DeploymentManifestError, "declaration capability is missing"):
            create_deployment_manifest(
                self.base / "missing-capability.json",
                active_artifact=wheel,
                rollback_artifact=self.rollback,
                configuration=self.config,
                state_backup=self.backup,
            )


if __name__ == "__main__":
    unittest.main()
