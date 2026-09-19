import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.hermes_plugin_compatibility import check_plugin_source


class HermesPluginCompatibilityTests(unittest.TestCase):
    def test_bootstrap_refuses_existing_unit_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            unit = config / "systemd/user/agents-projects-hub.service"
            unit.parent.mkdir(parents=True)
            unit.write_text("existing deployment\n")
            data = root / "data"
            result = subprocess.run(
                ["bash", str(Path(__file__).resolve().parents[1] / "scripts/install.sh")],
                env={**os.environ, "XDG_CONFIG_HOME": str(config), "XDG_DATA_HOME": str(data)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(unit.read_text(), "existing deployment\n")
            self.assertFalse(data.exists())

    def test_clean_matching_release_and_schema_are_both_required(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = source / "hermes_codex_router"
            package.mkdir()
            for clean, revision, maximum, expected in (
                (True, "a" * 40, 30, True),
                (True, "b" * 40, 30, False),
                (False, "a" * 40, 30, False),
                (True, "a" * 40, 29, False),
            ):
                with self.subTest(clean=clean, revision=revision, maximum=maximum):
                    (package / "_build_info.py").write_text(
                        f"CLEAN_TREE={clean!r}\nGIT_SHA={revision!r}\n"
                    )
                    (package / "schema_compatibility.py").write_text(
                        f"MIN_SUPPORTED_SCHEMA_VERSION=1\nMAX_SUPPORTED_SCHEMA_VERSION={maximum}\n"
                    )
                    self.assertEqual(check_plugin_source(source, 30, "a" * 40).ok, expected)

    def test_never_imports_plugin_code_or_reports_raw_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "hermes_codex_router"
            package.mkdir()
            marker = root / "should-not-exist"
            (package / "_build_info.py").write_text(
                f'raise RuntimeError("private")\nopen({str(marker)!r}, "w").write("ran")\n'
            )
            result = check_plugin_source(root, 30, "a" * 40)
            self.assertFalse(result.ok)
            self.assertFalse(marker.exists())
            self.assertNotIn("private", result.detail)
