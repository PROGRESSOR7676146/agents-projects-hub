"""Explicit opt-in native CLI compatibility; no provider/account inference."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import tempfile
from pathlib import Path
from typing import Any

from hermes_codex_router import claude_native_settings, claude_stream
from tests.claude_native_transport_actor import CASES, FAILURE_CATEGORIES, STAGES
from tests.native_process_capture import NativeCaptureError, capture_owned_process
from tests.native_runtime_mounts import native_runtime_mounts


class NativeTransportFixtureError(RuntimeError):
    """A bounded fixed-code compatibility failure, never native raw output."""


def _copy_native_binary(source: Path, destination: Path) -> str:
    source = source.resolve(strict=True)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not before.st_mode & 0o111
            or not 4 <= before.st_size <= 512 * 1024 * 1024
            or os.read(descriptor, 4) != b"\x7fELF"
        ):
            raise NativeTransportFixtureError("explicit_standalone_native_binary_required")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        copied = 0
        with destination.open("xb") as output:
            while chunk := os.read(descriptor, 1024 * 1024):
                copied += len(chunk)
                if copied > before.st_size:
                    raise NativeTransportFixtureError("native_binary_changed_during_copy")
                output.write(chunk)
                digest.update(chunk)
        after = os.fstat(descriptor)
        named = source.stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns")
        if copied != before.st_size or any(
            getattr(before, field) != getattr(candidate, field)
            for candidate in (after, named)
            for field in fields
        ):
            raise NativeTransportFixtureError("native_binary_changed_during_copy")
        destination.chmod(0o500)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def validate_transport_evidence(report: object, case: str) -> dict[str, Any]:
    if case not in CASES or not isinstance(report, dict):
        raise NativeTransportFixtureError("native_evidence_shape_invalid")
    if report.get("case") != case or not all(
        report.get(key) is True
        for key in ("host_files_hidden", "host_loopback_blocked", "ports_distinct")
    ):
        raise NativeTransportFixtureError("native_evidence_isolation_unproven")
    if (
        type(report.get("posts")) is not int
        or report["posts"] != 1
        or type(report.get("messages_served")) is not int
        or report["messages_served"] != 1
        or type(report.get("heads")) is not int
        or not 0 <= report["heads"] <= 1
        or type(report.get("requests")) is not int
        or report["requests"] != report["posts"] + report["heads"]
        or type(report.get("violations")) is not int
        or report["violations"] != 0
        or type(report.get("connections")) is not int
        or not 1 <= report["connections"] <= report["requests"]
        or type(report.get("timeouts")) is not int
        or report["timeouts"] != 0
    ):
        raise NativeTransportFixtureError("native_request_surface_unproven")
    rejected = case.endswith("reject")
    if type(report.get("exit_code")) is not int or any(
        type(report.get(key)) is not bool for key in ("terminal_success", "terminal_failure")
    ):
        raise NativeTransportFixtureError("native_terminal_shape_invalid")
    if rejected:
        valid = (
            report["exit_code"] != 0
            and report["terminal_failure"]
            and not report["terminal_success"]
            and report.get("failure_code")
            in {"claude_provider_overloaded", "claude_provider_failure"}
        )
    else:
        valid = (
            report["exit_code"] == 0
            and report["terminal_success"]
            and not report["terminal_failure"]
            and report.get("failure_code") is None
        )
    if not valid:
        raise NativeTransportFixtureError("native_terminal_outcome_unproven")
    return report


def run_native_transport_case(executable: Path, case: str) -> dict[str, Any]:
    if case not in CASES:
        raise NativeTransportFixtureError("native_case_invalid")
    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.is_file():
        raise NativeTransportFixtureError("native_namespace_unavailable")
    with tempfile.TemporaryDirectory(prefix="example-native-claude-") as directory:
        base = Path(directory)
        binary = base / "example-claude"
        digest = _copy_native_binary(executable, binary)
        sentinel = base / "fictional-authority"
        sentinel.write_text("example-only", encoding="utf-8")
        sentinel.chmod(0o600)
        if sentinel.read_text(encoding="utf-8") != "example-only":
            raise NativeTransportFixtureError("host_file_positive_control_failed")
        empty = base / "empty.py"
        empty.write_bytes(b"")
        actor = Path(__file__).with_name("claude_native_transport_actor.py")
        capture = Path(__file__).with_name("native_process_capture.py")
        parser = Path(claude_stream.__file__)
        settings = Path(claude_native_settings.__file__)
        with socket.socket() as listener:
            listener.settimeout(2)
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port), timeout=2) as control:
                connection, _address = listener.accept()
                with connection:
                    connection.settimeout(2)
                    control.sendall(b"example")
                    if connection.recv(7) != b"example":
                        raise NativeTransportFixtureError("host_network_positive_control_failed")
                    connection.sendall(b"ok")
                    if control.recv(2) != b"ok":
                        raise NativeTransportFixtureError("host_network_positive_control_failed")
            argv = [
                str(bwrap),
                "--die-with-parent",
                "--new-session",
                "--unshare-net",
                "--unshare-pid",
                "--unshare-ipc",
                *native_runtime_mounts(),
                "--tmpfs",
                "/tmp",
                "--dir",
                "/home/example/.claude",
                "--dir",
                "/home/example/.config",
                "--dir",
                "/home/example/.cache",
                "--dir",
                "/home/example/.local/share",
                "--dir",
                "/home/example/.local/state",
                "--dir",
                "/workspace/example",
                "--ro-bind",
                str(binary),
                "/opt/example/claude",
                "--ro-bind",
                str(actor),
                "/opt/example/actor.py",
                "--ro-bind",
                str(capture),
                "/opt/example/tests/native_process_capture.py",
                "--ro-bind",
                str(empty),
                "/opt/example/tests/__init__.py",
                "--ro-bind",
                str(parser),
                "/opt/example/hermes_codex_router/claude_stream.py",
                "--ro-bind",
                str(settings),
                "/opt/example/hermes_codex_router/claude_native_settings.py",
                "--ro-bind",
                str(empty),
                "/opt/example/hermes_codex_router/__init__.py",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                "/workspace/example",
                "--",
                "/usr/bin/python3",
                "-I",
                "/opt/example/actor.py",
                str(port),
                str(sentinel),
                case,
            ]
            try:
                code, output = capture_owned_process(
                    argv,
                    {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                    timeout=105,
                    stdout_limit=8192,
                    stderr_limit=65536,
                )
            except NativeCaptureError as error:
                raise NativeTransportFixtureError("native_namespace_capture_failed") from error
            if code != 0:
                try:
                    failure = json.loads(output)
                except (ValueError, RecursionError):
                    failure = None
                if (
                    isinstance(failure, dict)
                    and failure.get("fixture_failed") is True
                    and failure.get("stage") in STAGES
                    and failure.get("category") in FAILURE_CATEGORIES
                ):
                    raise NativeTransportFixtureError(
                        "native_namespace_execution_failed:"
                        + failure["stage"]
                        + ":"
                        + failure["category"]
                    )
                raise NativeTransportFixtureError("native_namespace_execution_failed")
            try:
                report = json.loads(output)
            except (ValueError, RecursionError) as error:
                raise NativeTransportFixtureError("native_evidence_shape_invalid") from error
            validated = validate_transport_evidence(report, case)
            validated["native_binary_sha256"] = digest
            return validated
