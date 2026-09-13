from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from hermes_codex_router.codex_proxy_health import (
    probe_codex_config_proxy,
    probe_codex_multi_auth_accounts,
    probe_codex_runtime_proxy,
)


class Connection:
    def close(self) -> None:
        pass


class CodexRuntimeProxyHealthTests(unittest.TestCase):
    def test_requires_advertised_reachable_dynamic_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            process = proc / "123"
            process.mkdir()
            (process / "cmdline").write_bytes(
                b"codex\x00-c\x00"
                b'model_providers.codex-multi-auth-runtime-proxy.base_url="'
                b'http://127.0.0.1:35411"\x00'
            )
            calls: list[tuple[tuple[str, int], float]] = []

            def connect(address: tuple[str, int], *, timeout: float) -> Any:
                calls.append((address, timeout))
                return Connection()

            result = probe_codex_runtime_proxy(proc_root=proc, connect=connect)
            self.assertTrue(result.ok)
            self.assertEqual(calls, [(("127.0.0.1", 35411), 1.0)])

    def test_rejects_missing_or_unreachable_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            self.assertFalse(probe_codex_runtime_proxy(proc_root=proc).ok)
            process = proc / "456"
            process.mkdir()
            (process / "cmdline").write_bytes(
                b"codex\x00-c\x00"
                b"model_providers.codex-multi-auth-runtime-proxy.base_url="
                b"http://127.0.0.1:45789\x00"
            )

            def unavailable(*_args: object, **_kwargs: object) -> Any:
                raise OSError("closed")

            self.assertFalse(probe_codex_runtime_proxy(proc_root=proc, connect=unavailable).ok)

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

    def test_multi_auth_accounts_detects_invalid_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "settings.json").write_text(
                json.dumps({"pluginConfig": {"codexRuntimeRotationProxy": True}})
            )
            (base / "quota-cache.json").write_text(json.dumps({"byAccountId": {}}))
            invalid_report = {
                "forecast": {
                    "accounts": [
                        {
                            "index": 1,
                            "label": "redacted account",
                            "availability": "unavailable",
                            "riskLevel": "high",
                            "reasons": ["token-invalid — re-login needed"],
                        }
                    ]
                },
                "runtime": {"runtimeMetrics": {}},
            }

            def invalid_runner(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess([], 0, json.dumps(invalid_report), "")

            result = probe_codex_multi_auth_accounts(base, runner=invalid_runner)
            self.assertFalse(result.ok)
            self.assertIn("2", result.detail)

            invalid_report["forecast"]["accounts"][0]["reasons"] = []
            self.assertTrue(probe_codex_multi_auth_accounts(base, runner=invalid_runner).ok)


if __name__ == "__main__":
    unittest.main()
