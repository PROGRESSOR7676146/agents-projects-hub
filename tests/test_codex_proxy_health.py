from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from hermes_codex_router.codex_proxy_health import probe_codex_runtime_proxy


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


if __name__ == "__main__":
    unittest.main()
