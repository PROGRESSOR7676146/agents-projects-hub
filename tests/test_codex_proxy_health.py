from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from hermes_codex_router.codex_proxy_health import probe_codex_config_proxy


class Connection:
    def close(self) -> None:
        pass


class CodexConfigProxyHealthTests(unittest.TestCase):
    def test_config_proxy_direct_or_missing_file_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_path = Path(directory) / "config.toml"
            result = probe_codex_config_proxy(missing_path)
            self.assertTrue(result.ok)

            config_file = Path(directory) / "default.toml"
            config_file.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
            result = probe_codex_config_proxy(config_file)
            self.assertTrue(result.ok)

    def test_config_proxy_reachable_custom_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.toml"
            config_file.write_text(
                'model_provider = "custom-proxy"\n'
                "[model_providers.custom-proxy]\n"
                'base_url = "http://127.0.0.1:42911"\n',
                encoding="utf-8",
            )
            calls: list[tuple[str, int]] = []

            def connect(address: tuple[str, int], *, timeout: float) -> Any:
                calls.append(address)
                return Connection()

            result = probe_codex_config_proxy(config_file, connect=connect)
            self.assertTrue(result.ok)
            self.assertEqual(calls, [("127.0.0.1", 42911)])

    def test_config_proxy_unreachable_custom_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.toml"
            config_file.write_text(
                'model_provider = "custom-proxy"\n'
                "[model_providers.custom-proxy]\n"
                'base_url = "http://127.0.0.1:42911"\n',
                encoding="utf-8",
            )

            def unavailable(*_args: object, **_kwargs: object) -> Any:
                raise OSError("connection refused")

            result = probe_codex_config_proxy(config_file, connect=unavailable)
            self.assertFalse(result.ok)
            self.assertIn("unreachable", result.detail)

    def test_config_proxy_does_not_probe_or_expose_remote_or_secret_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.toml"
            config_file.write_text(
                'model_provider = "custom-proxy"\n'
                "[model_providers.custom-proxy]\n"
                'base_url = "https://user:secret@example.com/v1?token=secret"\n',
                encoding="utf-8",
            )

            def unexpected_connect(*_args: object, **_kwargs: object) -> Any:
                raise AssertionError("remote endpoints must not be probed")

            result = probe_codex_config_proxy(config_file, connect=unexpected_connect)
            self.assertTrue(result.ok)
            self.assertNotIn("secret", result.detail)
            self.assertNotIn("example.com", result.detail)


if __name__ == "__main__":
    unittest.main()
