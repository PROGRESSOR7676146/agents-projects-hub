"""Real private namespace pipe/HTTP witness using only fictional materials."""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from hermes_codex_router.process_namespace import (
    NamespaceError,
    NamespaceRuntime,
    ProcessNamespaceConfig,
)
from hermes_codex_router.review_bridge_attempt import BridgeAttemptGate, BridgeAttemptSpec
from hermes_codex_router.review_materials import MaterialSelection, build_review_capsule
from tests.fd_fixture import assert_descriptor_cleanup
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_namespace_runtime,
)
from tests.review_bridge_pipe_fixture import PipeFixtureResult, actor_argv, run_pipe_fixture


class ReviewBridgeNamespaceTests(unittest.TestCase):
    def run_case(self, scenario: str, *, deny_inherited_stdin: bool = False) -> PipeFixtureResult:
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python = Path("/usr/bin/python3.12")
        roots = (
            python,
            Path("/usr/lib/python3.12"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"),
        )
        if not all(path.exists() for path in roots):
            namespace_unavailable(self, "system Python runtime fixture unavailable")
        try:
            runtime = NamespaceRuntime(bwrap, python, roots)
        except NamespaceError:
            namespace_unavailable(self, "system runtime is not immutable to worker")
        with tempfile.TemporaryDirectory(prefix="example-review-http-") as directory:
            base = Path(directory)
            original, skeleton, home, authority = (
                base / name for name in ("original", "skeleton", "session", "authority")
            )
            for path in (original, skeleton, home, authority):
                path.mkdir(mode=0o700)
            for path in (original, skeleton):
                (path / ".git").mkdir()
            selected = original / "visible.txt"
            content = b"fictional-authorized-review-material"
            selected.write_bytes(content)
            (original / ".git/HEAD").write_text("fictional-private-git")
            secret = authority / "key"
            secret.write_text("fictional-authority-key")
            selection = MaterialSelection(
                "visible.txt", len(content), hashlib.sha256(content).hexdigest()
            )
            with (
                assert_descriptor_cleanup(self),
                socket.socket() as tcp,
                socket.socket(socket.AF_UNIX) as pathname,
                socket.socket(socket.AF_UNIX) as abstract,
                secret.open("rb") as handle,
                build_review_capsule(original, (selection,), binding="example-result") as capsule,
            ):
                address = str(authority / "hidden.sock")
                abstract_name = "example-review-" + uuid4().hex
                tcp.bind(("127.0.0.1", 0))
                pathname.bind(address)
                abstract.bind("\0" + abstract_name)
                for server in (tcp, pathname, abstract):
                    server.listen(1)
                    server.settimeout(1)
                for family, endpoint, server in (
                    (socket.AF_INET, tcp.getsockname(), tcp),
                    (socket.AF_UNIX, address, pathname),
                    (socket.AF_UNIX, "\0" + abstract_name, abstract),
                ):
                    with socket.socket(family) as control:
                        control.settimeout(1)
                        control.connect(endpoint)
                        connected, _ = server.accept()
                        with connected:
                            connected.sendall(b"example-positive")
                            self.assertEqual(control.recv(64), b"example-positive")
                response = os.urandom(64000)
                calls: list[bytes] = []

                def callback(body: bytes) -> bytes:
                    calls.append(body)
                    return response

                spec = BridgeAttemptSpec(
                    "example-attempt",
                    "example-result",
                    capsule.digest,
                    capsule.size,
                    "00000000-0000-4000-8000-000000000001",
                    "example-model",
                    "high",
                    1024,
                    hashlib.sha256(capsule.read()).hexdigest(),
                )
                gate = BridgeAttemptGate(spec, capsule, upstream=callback)
                config = ProcessNamespaceConfig(runtime, skeleton, home, (original, authority))
                argv = actor_argv(str(python))
                self.assertNotIn(content.decode(), " ".join(argv))
                descriptors = (
                    handle.fileno(),
                    capsule.fileno(),
                    tcp.fileno(),
                    pathname.fileno(),
                    abstract.fileno(),
                )
                for descriptor in descriptors:
                    os.set_inheritable(descriptor, True)
                with config.wrap(argv, {}, skeleton) as launch:
                    self.assertTrue(set(descriptors).isdisjoint(launch.pass_fds))
                    inputs = {
                        "hidden": [
                            str(selected),
                            str(original / ".git/HEAD"),
                            str(secret),
                            ".git/HEAD",
                        ],
                        "denied_inodes": [
                            [info.st_dev, info.st_ino]
                            for info in (os.fstat(fd) for fd in (*descriptors, *launch.pass_fds))
                        ],
                        "host_port": tcp.getsockname()[1],
                        "pathname": address,
                        "abstract": abstract_name,
                        "host_netns": os.readlink("/proc/self/ns/net"),
                    }
                    result = run_pipe_fixture(
                        list(launch.argv),
                        launch.environment,
                        gate,
                        pass_fds=launch.pass_fds,
                        inputs=inputs,
                        scenario=scenario,
                        timeout=4,
                        deny_inherited_stdin=deny_inherited_stdin,
                    )
                if result.stderr and namespace_permission_refused(result.stderr.decode()):
                    namespace_unavailable(self, "kernel disallows user namespaces")
                self.assertEqual(selected.read_bytes(), content)
                self.assertEqual(secret.read_text(), "fictional-authority-key")
                self.assertFalse((skeleton / ".git/new").exists())
                self.assertTrue(result.cleanup_eof, result.error)
                self.assertEqual(len(calls), int(result.attempted))
                if result.success:
                    self.assertEqual(calls, [capsule.read()])
                    self.assertEqual(
                        result.receipt["response_sha256"], hashlib.sha256(response).hexdigest()
                    )
                    self.assertIs(result.receipt["isolated"], True)
                    for role in ("parent", "exec"):
                        self.assertEqual(
                            (home / (role + "-control")).read_text(), "example-private-session"
                        )
                return result

    def test_private_pipe_http_delivery_and_parent_exec_isolation(self) -> None:
        result = self.run_case("success")
        self.assertTrue(result.success, result.error)
        self.assertTrue(result.transport_closed)
        self.assertTrue(result.drained)

    def test_namespace_teardown_closes_setsid_descendant_pipe(self) -> None:
        result = self.run_case("escaped_pipe")
        self.assertFalse(result.success)
        self.assertFalse(result.attempted)
        self.assertTrue(result.escaped_ready)
        self.assertIn(result.error, ("example_pipe_deadline", "bridge_sequence_eof_incomplete"))
        self.assertLess(result.elapsed, 8)
        self.assertTrue(result.cleanup_eof)

    def test_inherited_pipe_inode_positive_control_detects_descriptor_leak(self) -> None:
        result = self.run_case("success", deny_inherited_stdin=True)
        self.assertFalse(result.success)
        self.assertFalse(result.attempted)
        self.assertIn(b"example_descriptor_leak", result.stderr)
        self.assertNotIn(b"example_actor_deadline", result.stderr)
        self.assertTrue(result.cleanup_eof)


if __name__ == "__main__":
    unittest.main()
